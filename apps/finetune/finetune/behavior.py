"""What the adapter is meant to change, measured on text alone.

Cheap, deterministic checks with no model call, used twice: generate.py
drops teacher replies in the wrong language, and evaluate.py scores the
model's answers on the validation set before and after fine-tuning. They're
heuristics, good enough to tell English from Spanish and a refusal from an
answer; the Claude judge (apps/rag/eval/judge/) is the real measure.
"""
import re

STOPWORDS = {
    "en": {"the", "is", "are", "and", "of", "to", "it", "with", "for", "that", "which", "this", "because"},
    "es": {"el", "la", "los", "las", "es", "son", "y", "de", "que", "con", "para", "por", "porque", "una"},
}

# Generous on purpose: "the context doesn't provide it. However, ..." counts
# as declining, as a hedge Mistral-7B often writes; the judge is stricter.
_DECLINE = re.compile(
    r"don't know|do not know"
    r"|(?:don't|doesn't|do not|does not) (?:contain|cover|mention|include|provide|explain)"
    r"|(?:can't|cannot) (?:directly )?(?:answer|provide)|not (?:directly )?given"
    r"|not (?:mentioned|covered|included) in"
    r"|no (?:lo )?sé|no puedo responder|no dispongo"
    r"|no (?:contiene|cubre|menciona|incluye|trata|indica|explica|dice)n?\b",
    re.IGNORECASE,
)
_CITATION = re.compile(r"\[\d+\]")


def detected_language(text: str) -> str:
    """en or es, by stopwords outside code spans: enough to catch an answer
    in the wrong one of two languages."""
    words = re.findall(r"[a-záéíóúñü]+", re.sub(r"`[^`]*`", " ", text.lower()))
    en, es = (sum(w in STOPWORDS[lang] for w in words) for lang in ("en", "es"))
    return "es" if es > en else "en"


def declines(text: str) -> bool:
    """Whether the answer says the context doesn't have it."""
    return bool(_DECLINE.search(text))


def cites(text: str) -> bool:
    return bool(_CITATION.search(text))


def score(rows: list[dict]) -> dict:
    """Behavior metrics over rows with lang, answerable and answer.

    language: answers in the question's language
    declines: unanswerable questions answered with a refusal
    false_declines: answerable questions refused (lower is better)
    cites: answerable questions whose answer cites a passage
    """
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]

    def rate(xs, pred):
        return round(sum(map(pred, xs)) / len(xs), 3) if xs else 0.0

    return {
        "language": rate(rows, lambda r: detected_language(r["answer"]) == r["lang"]),
        "language_es": rate([r for r in rows if r["lang"] == "es"],
                            lambda r: detected_language(r["answer"]) == "es"),
        "declines": rate(unanswerable, lambda r: declines(r["answer"])),
        "false_declines": rate(answerable, lambda r: declines(r["answer"])),
        "cites": rate(answerable, lambda r: cites(r["answer"])),
    }
