import pytest

from finetune import data


class CharTokenizer:
    """One token per character, Mistral-style turns: enough to check the mask."""

    def __init__(self, dict_output=False, break_prefix=False):
        self.dict_output = dict_output
        self.break_prefix = break_prefix

    def apply_chat_template(self, messages, tokenize, add_generation_prompt):
        text = "<s>"
        for m in messages:
            text += f"[INST] {m['content']} [/INST]" if m["role"] == "user" else m["content"] + "</s>"
        if self.break_prefix and not add_generation_prompt:
            text = "X" + text
        ids = [ord(c) for c in text]
        return {"input_ids": ids} if self.dict_output else ids


EXAMPLE = {"id": "ex1", "messages": [{"role": "user", "content": "Question?"},
                                     {"role": "assistant", "content": "Answer [1]."}]}


@pytest.mark.parametrize("dict_output", [False, True])
def test_encode_masks_the_prompt(dict_output):
    tok = CharTokenizer(dict_output)
    out = data.encode(tok, EXAMPLE, max_len=1000)
    prompt = "<s>[INST] Question? [/INST]"
    assert out["input_ids"] == [ord(c) for c in prompt + "Answer [1].</s>"]
    assert out["labels"][:len(prompt)] == [data.IGNORE] * len(prompt)
    assert "".join(chr(t) for t in out["labels"][len(prompt):]) == "Answer [1].</s>"


def test_encode_skips_too_long():
    assert data.encode(CharTokenizer(), EXAMPLE, max_len=10) is None


def test_encode_rejects_a_misaligned_mask():
    with pytest.raises(ValueError, match="prefix"):
        data.encode(CharTokenizer(break_prefix=True), EXAMPLE, max_len=1000)


def test_collate_pads_and_masks():
    batch = data.collate([{"input_ids": [1, 2, 3], "labels": [-100, 2, 3]},
                          {"input_ids": [4], "labels": [4]}], pad_id=0)
    assert batch["input_ids"].tolist() == [[1, 2, 3], [4, 0, 0]]
    assert batch["labels"].tolist() == [[-100, 2, 3], [4, -100, -100]]
    assert batch["attention_mask"].tolist() == [[1, 1, 1], [1, 0, 0]]


def test_dataset_examples_are_chats():
    for split in ("train", "validation"):
        for ex in data.load("data/dataset", split):
            roles = [m["role"] for m in ex["messages"]]
            assert roles == ["user", "assistant"], ex["id"]
            assert ex["messages"][1]["content"] == ex["answer"]
