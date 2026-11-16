"""Dataset loading and tokenization for supervised fine-tuning.

Each example is a chat: the RAG prompt as the user turn, the reference
answer as the assistant turn. Both are rendered with the model's own chat
template, the one vLLM applies when it serves the model, and the loss only
counts the answer's tokens: the model learns what to reply, not to recite
the docs it's given.
"""
import json
from pathlib import Path

# Mistral-7B-Instruct v0.2's template, for tokenizers that ship without one
# (the tiny test model). The real model's tokenizer has its own.
MISTRAL_TEMPLATE = (
    "{{ bos_token }}{% for message in messages %}"
    "{% if message['role'] == 'user' %}{{ '[INST] ' + message['content'] + ' [/INST]' }}"
    "{% elif message['role'] == 'assistant' %}{{ message['content'] + eos_token }}{% endif %}"
    "{% endfor %}"
)

IGNORE = -100  # label value the loss skips


def load(dataset_dir: str, split: str) -> list[dict]:
    with open(Path(dataset_dir) / f"{split}.jsonl") as f:
        return [json.loads(line) for line in f]


def _ids(tokenizer, messages: list[dict], generation_prompt: bool) -> list[int]:
    out = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=generation_prompt)
    # transformers 5 returns a BatchEncoding here; 4.x returned the list.
    return list(out["input_ids"] if hasattr(out, "keys") else out)


def prompt_ids(tokenizer, example: dict) -> list[int]:
    """The user turn, ready for the model to answer (generation)."""
    return _ids(tokenizer, example["messages"][:1], generation_prompt=True)


def encode(tokenizer, example: dict, max_len: int) -> dict | None:
    """input_ids and labels, with the prompt masked out; None if too long.

    The answer tokens are the full chat minus the prompt. That only holds
    if the prompt's tokens are a prefix of the full chat's, which a chat
    template could break at the boundary: such examples are rejected
    rather than trained with a misaligned mask.
    """
    prompt = prompt_ids(tokenizer, example)
    full = _ids(tokenizer, example["messages"][:2], generation_prompt=False)
    if full[:len(prompt)] != prompt:
        raise ValueError(f"{example['id']}: the prompt isn't a prefix of the full chat")
    if len(full) > max_len:
        return None
    return {"input_ids": full, "labels": [IGNORE] * len(prompt) + full[len(prompt):]}


def collate(batch: list[dict], pad_id: int) -> dict:
    """Right-pads a batch; padding is masked from attention and loss."""
    import torch

    width = max(len(b["input_ids"]) for b in batch)
    pad = lambda xs, v: xs + [v] * (width - len(xs))  # noqa: E731
    return {
        "input_ids": torch.tensor([pad(b["input_ids"], pad_id) for b in batch]),
        "labels": torch.tensor([pad(b["labels"], IGNORE) for b in batch]),
        "attention_mask": torch.tensor([pad([1] * len(b["input_ids"]), 0) for b in batch]),
    }
