"""Behavior check of a registered adapter, with no API call.

Downloads the adapter of a model version from MLflow, answers the
validation split with the base model and with the adapter (one model load:
the adapter is switched off for the base), and scores both with
finetune.behavior: answers in the question's language, refusals when the
context lacks the answer, citations. The metrics and every generation go
to the training run of that version, so MLflow shows before and after side
by side.

It's a quick, free signal, not the verdict: answers here come from the
dataset's passages, not from the live RAG service, and the checks are
heuristics. Promotion is decided by the Claude judge on real answers.

    python -m finetune.evaluate --base-model ... --version 3 [--limit 8]
"""
import argparse
import json
import time

from . import behavior, data
from .train import load_model, load_tokenizer


def generate(model, tok, examples: list[dict], max_new_tokens: int, batch_size: int) -> list[str]:
    import torch

    answers = []
    for i in range(0, len(examples), batch_size):
        prompts = [data.prompt_ids(tok, ex) for ex in examples[i:i + batch_size]]
        width = max(map(len, prompts))
        # Left padding, so every prompt ends where generation starts.
        ids = torch.tensor([[tok.pad_token_id] * (width - len(p)) + p for p in prompts])
        mask = torch.tensor([[0] * (width - len(p)) + [1] * len(p) for p in prompts])
        with torch.no_grad():
            out = model.generate(input_ids=ids.to(model.device), attention_mask=mask.to(model.device),
                                 max_new_tokens=max_new_tokens, do_sample=False,
                                 pad_token_id=tok.pad_token_id)
        answers += [tok.decode(o[width:], skip_special_tokens=True).strip() for o in out]
    return answers


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-model", required=True)
    p.add_argument("--dataset-dir", default="/data")
    p.add_argument("--model-name", default="rag-behavior-lora")
    p.add_argument("--version", required=True, help="model version, or an alias like candidate")
    p.add_argument("--split", default="validation")
    p.add_argument("--limit", type=int, help="only the first N examples (smoke test)")
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--batch-size", type=int, default=4)
    args = p.parse_args()

    import mlflow
    import torch
    from mlflow import MlflowClient
    from peft import PeftModel

    client = MlflowClient()
    mv = (client.get_model_version(args.model_name, args.version) if args.version.isdigit()
          else client.get_model_version_by_alias(args.model_name, args.version))
    adapter = mlflow.artifacts.download_artifacts(artifact_uri=mv.source)

    cuda = torch.cuda.is_available()
    tok = load_tokenizer(args.base_model)
    model = PeftModel.from_pretrained(load_model(args.base_model, cuda, for_training=False), adapter)
    model.eval()

    examples = data.load(args.dataset_dir, args.split)[:args.limit]
    results, generations = {}, {}
    for variant in ("base", "adapter"):
        start = time.monotonic()
        if variant == "base":
            with model.disable_adapter():
                answers = generate(model, tok, examples, args.max_new_tokens, args.batch_size)
        else:
            answers = generate(model, tok, examples, args.max_new_tokens, args.batch_size)
        rows = [{"id": ex["id"], "lang": ex["lang"], "answerable": ex["answerable"],
                 "question": ex["question"], "answer": a} for ex, a in zip(examples, answers)]
        results[variant] = {**behavior.score(rows), "seconds": round(time.monotonic() - start, 1)}
        generations[variant] = rows

    run_id = mv.run_id
    for variant, metrics in results.items():
        for k, v in metrics.items():
            client.log_metric(run_id, f"behavior_{variant}_{k}", v)
    client.log_dict(run_id, generations, f"behavior/{args.split}-generations.json")
    for k, v in results["adapter"].items():
        client.set_model_version_tag(args.model_name, mv.version, f"behavior_{k}", str(v))
    print(json.dumps({"model_version": mv.version, "examples": len(examples), **results}, indent=2))


if __name__ == "__main__":
    main()
