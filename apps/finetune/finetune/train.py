"""LoRA fine-tuning of the served model on the RAG behavior dataset.

On a GPU (the T4 on EKS) it's QLoRA: the base model loaded in 4-bit NF4,
fp16 compute (the T4 has no bf16), LoRA adapters on every attention and MLP
projection. On CPU it trains in fp32, which is only useful for the smoke
test with a tiny model: the same code path, data and MLflow logging in
seconds.

Everything goes to MLflow (MLFLOW_TRACKING_URI): parameters, train and
validation loss, the adapter as an artifact, and a new version of the
registered model with the alias `candidate`. Promotion to `champion`, what
vLLM serves, is a separate, gated step.

    python -m finetune.train --base-model mistralai/Mistral-7B-Instruct-v0.2 \\
        --dataset-dir /data [--epochs 2] [--version-file /tmp/outputs/version]
"""
import argparse
import functools
import hashlib
import os
import tempfile
import time
from pathlib import Path

from . import data

# Every linear projection of a Mistral (Llama-style) block; vLLM can serve
# LoRA on all of them.
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def load_model(base_model: str, cuda: bool, for_training: bool):
    import torch
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig

    if not cuda:
        return AutoModelForCausalLM.from_pretrained(base_model, dtype=torch.float32)
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                               bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(base_model, quantization_config=quant,
                                                 dtype=torch.float16, device_map={"": 0})
    if for_training:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    return model


def load_tokenizer(base_model: str):
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(base_model)
    if not tok.chat_template:
        tok.chat_template = data.MISTRAL_TEMPLATE
    if tok.pad_token_id is None:  # Mistral has no pad token
        tok.pad_token = tok.unk_token or tok.eos_token
    return tok


def dataset_hash(dataset_dir: str) -> str:
    h = hashlib.sha256()
    for name in ("train.jsonl", "validation.jsonl"):
        h.update((Path(dataset_dir) / name).read_bytes())
    return h.hexdigest()[:12]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-model", required=True)
    p.add_argument("--dataset-dir", default="/data")
    p.add_argument("--model-name", default="rag-behavior-lora", help="registered model in MLflow")
    p.add_argument("--experiment", default="rag-behavior-lora")
    p.add_argument("--epochs", type=float, default=2)
    p.add_argument("--max-steps", type=int, default=-1, help="cap the steps (smoke test)")
    p.add_argument("--learning-rate", type=float, default=2e-4)
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--max-len", type=int, default=2048)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--version-file", help="write the registered version here (an Argo output)")
    args = p.parse_args()

    import mlflow
    import torch
    from mlflow import MlflowClient
    from peft import LoraConfig, get_peft_model
    from transformers import Trainer, TrainingArguments

    cuda = torch.cuda.is_available()
    tok = load_tokenizer(args.base_model)
    splits, skipped = {}, {}
    for split in ("train", "validation"):
        encoded = [data.encode(tok, ex, args.max_len) for ex in data.load(args.dataset_dir, split)]
        splits[split] = [e for e in encoded if e is not None]
        skipped[split] = len(encoded) - len(splits[split])
    print(f"cuda={cuda} train={len(splits['train'])} validation={len(splits['validation'])} "
          f"skipped (too long)={skipped}", flush=True)

    model = load_model(args.base_model, cuda, for_training=True)
    model = get_peft_model(model, LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
        target_modules=TARGET_MODULES, task_type="CAUSAL_LM"))
    trainable, total = model.get_nb_trainable_parameters()

    mlflow.set_experiment(args.experiment)
    work = Path(tempfile.mkdtemp())
    with mlflow.start_run() as run:
        mlflow.log_params({
            "base_model": args.base_model, "dataset_hash": dataset_hash(args.dataset_dir),
            "train_examples": len(splits["train"]), "validation_examples": len(splits["validation"]),
            "skipped_too_long": sum(skipped.values()), "quantization": "nf4" if cuda else "none",
            "lora_r": args.lora_r, "lora_alpha": args.lora_alpha, "lora_dropout": args.lora_dropout,
            "target_modules": ",".join(TARGET_MODULES), "trainable_params": trainable,
            "total_params": total, "image_version": os.environ.get("FINETUNE_VERSION", "dev"),
            "device": torch.cuda.get_device_name(0) if cuda else "cpu",
        })
        trainer = Trainer(
            model=model,
            args=TrainingArguments(
                output_dir=str(work / "trainer"), num_train_epochs=args.epochs, max_steps=args.max_steps,
                per_device_train_batch_size=args.batch_size, per_device_eval_batch_size=args.batch_size,
                gradient_accumulation_steps=args.grad_accum, learning_rate=args.learning_rate,
                lr_scheduler_type="cosine", warmup_steps=0.05, logging_steps=5,
                eval_strategy="epoch" if args.max_steps < 0 else "no", save_strategy="no",
                fp16=cuda, gradient_checkpointing=cuda, remove_unused_columns=False,
                report_to=["mlflow"], seed=args.seed,
            ),
            train_dataset=splits["train"], eval_dataset=splits["validation"],
            data_collator=functools.partial(data.collate, pad_id=tok.pad_token_id),
        )
        start = time.monotonic()
        trainer.train()
        mlflow.log_metric("train_seconds", round(time.monotonic() - start, 1))
        mlflow.log_metrics({f"final_{k}": v for k, v in trainer.evaluate().items()
                            if isinstance(v, (int, float))})

        # Only the adapter: vLLM loads it on top of the base model it serves.
        adapter = work / "adapter"
        model.save_pretrained(adapter)
        mlflow.log_artifacts(str(adapter), "adapter")

        client = MlflowClient()
        if not client.search_registered_models(filter_string=f"name = '{args.model_name}'"):
            client.create_registered_model(args.model_name)
        version = client.create_model_version(
            args.model_name, source=f"{run.info.artifact_uri}/adapter", run_id=run.info.run_id,
            tags={"base_model": args.base_model})
        client.set_registered_model_alias(args.model_name, "candidate", version.version)
        print(f"registered {args.model_name} version {version.version} (alias candidate), "
              f"run {run.info.run_id}", flush=True)

    if args.version_file:
        Path(args.version_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.version_file).write_text(str(version.version))


if __name__ == "__main__":
    main()
