"""Builds the LoRA fine-tuning dataset: RAG prompts exactly as the API sends
them, with the answer a good model should give, written by Claude.

What the adapter should learn is behavior, not facts: answer in the
question's language (Mistral-7B answered 0 of 7 Spanish questions in
Spanish), say it doesn't know when the context lacks the answer, and cite
the passages it uses. So every example is a real retrieval:

1. Seeds: corpus chunks that contain none of eval/questions.yaml's
   evidence or questions, so no training question is about a fact the
   evaluation asks (neighboring chunks can still overlap a little).
2. Claude writes questions each seed answers, half English, half Spanish.
   Some get the seed removed from their context (should decline), and
   Claude writes off-topic and near-domain questions the docs can't answer.
3. Questions too similar to an evaluation question are dropped.
4. Each question is retrieved like /ask does (same chunking, embeddings,
   top 4, prompt from rag.prompt), and Claude answers that exact prompt.
5. Train/validation split by seed, so validation questions come from
   passages training never asked about.

Runs in the RAG image, which has the corpus, the embedding model and the
rag package (from the repo root; needs ANTHROPIC_API_KEY, costs money):

    docker run --rm -e ANTHROPIC_API_KEY -e PYTHONPATH=/app \\
      -v "$PWD/apps/finetune":/finetune -v "$PWD/apps/rag/eval":/eval:ro llm-platform-rag:dev \\
      python /finetune/data/generate.py --out /finetune/data/dataset [--seeds 3]

--seeds N is a pilot. If the account runs out of credit the run stops and
keeps what it has; --extend adds unanswerable questions to an existing
dataset, from the API or from --negatives-file (data/negatives.yaml, written
by hand, no API needed). That's how the committed dataset was built: the
API's run stopped after the seed questions.
"""
import argparse
import collections
import concurrent.futures
import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import yaml

from rag import embeddings
from rag.chunking import Chunk
from rag.config import settings
from rag.evaluate import COLLECTION, build_index
from rag.ingest import load_chunks
from rag.prompt import build_messages
from rag.scoring import norm

API = "https://api.anthropic.com/v1/messages"
LANGUAGES = {"en": "English", "es": "Spanish"}
_usage_lock = threading.Lock()

QUESTIONS_PROMPT = """\
Below is a passage from the documentation of llm-platform-on-eks, a demo \
platform that serves LLMs on Kubernetes (EKS) with vLLM, LiteLLM, KEDA, \
Karpenter, a RAG service, Argo CD and cost observability.

Source: {source} - {heading}
<passage>
{text}
</passage>

Write {n} questions a user of this platform might ask that this passage \
answers: {half} in English and {half} in Spanish (Spanish as a native speaker \
would ask it, keeping technical names as they are). Mix factual questions \
(numbers, names, settings) with why/how questions. Each one must be \
answerable from this passage alone and make sense on its own: never refer \
to "the passage", "the table" or "the text". No two questions may ask for \
the same fact.
"""

NEGATIVES_PROMPT = """\
A documentation assistant answers questions about llm-platform-on-eks, a \
demo platform that serves an LLM (Mistral-7B on vLLM) on AWS EKS, with a \
LiteLLM gateway, KEDA and Karpenter autoscaling, a RAG service over its own \
docs, Argo CD, Prometheus/Grafana/Tempo, and Kyverno/Trivy security.

Write {n} questions users might ask it that its documentation can't answer: \
{half} in English and {half} in Spanish. A third off-topic (cooking, sports, \
travel, history, today's news); a third general tech not about this project \
(programming languages, other clouds' services, laptop hardware); a third \
close to the project's vocabulary but about things it doesn't document \
(other inference servers' benchmarks, GKE or AKS setup, H100 prices, how to \
train a model from scratch, other companies' platforms). All different.
"""

TEACHER_PROMPT = """\
You are writing the reference answer a small model will be fine-tuned to \
give. Below, between the markers, is the exact prompt it receives: \
numbered passages retrieved from the project's docs, and a user question.

Answer it as the ideal assistant would:
- Use only the numbered passages, citing the ones you use as [1], [2].
- The question is in {language}: write the whole answer in {language}, \
whatever the passages' language. Keep technical names, settings and \
numbers as they are.
- If the passages don't contain the answer, say so in one sentence (in \
{language}) and stop: don't answer from general knowledge, even partly, \
and don't offer the closest related passage instead.
- Be direct: 1-4 sentences, no preamble, don't repeat the question.

=== PROMPT ===
{prompt}
=== END ==="""


QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {"lang": {"type": "string", "enum": ["en", "es"]}, "question": {"type": "string"}},
        "required": ["lang", "question"]}}},
    "required": ["questions"],
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "answerable": {"type": "boolean", "description": "whether the passages contained the answer"},
    },
    "required": ["answer", "answerable"],
}


class OutOfCredit(Exception):
    """The account ran out of credit: every later call fails too."""


def ask_claude(http: httpx.Client, model: str, prompt: str, schema: dict, max_tokens: int,
               usage: dict) -> dict:
    """One Messages API call. The reply is forced through a tool with a JSON
    schema, so it's always well-formed (free-text JSON broke on quotes
    inside questions). Retries rate limits and overload."""
    for attempt in range(6):
        r = http.post(API, json={
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"name": "reply", "description": "Submit the reply.", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "reply"},
        })
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 400 and "credit balance" in r.text:
            raise OutOfCredit(r.json()["error"]["message"])
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        with _usage_lock:
            for k in ("input_tokens", "output_tokens"):
                usage[k] = usage.get(k, 0) + body["usage"][k]
        return next(b["input"] for b in body["content"] if b["type"] == "tool_use")
    raise RuntimeError(f"gave up after retries: HTTP {r.status_code}")


def seed_chunks(chunks: list[Chunk], questions: list[dict], min_chars: int) -> list[Chunk]:
    """Chunks that contain no evaluation evidence, and don't quote an
    evaluation question either (docs/retrieval-eval.md quotes some)."""
    banned = [norm(e) for q in questions for e in q.get("evidence", [])]
    banned += [norm(q["question"]) for q in questions]
    return [c for c in chunks if len(c.text) >= min_chars
            and not any(b in norm(c.text) for b in banned)]


STOPWORDS = {
    "en": {"the", "is", "are", "and", "of", "to", "it", "with", "for", "that", "which", "this", "because"},
    "es": {"el", "la", "los", "las", "es", "son", "y", "de", "que", "con", "para", "por", "porque", "una"},
}


def detected_language(text: str) -> str:
    """en or es, by stopwords outside code spans: enough to catch an answer
    in the wrong one of two languages."""
    words = re.findall(r"[a-záéíóúñü]+", re.sub(r"`[^`]*`", " ", text.lower()))
    en, es = (sum(w in STOPWORDS[lang] for w in words) for lang in ("en", "es"))
    return "es" if es > en else "en"


def eval_similarity(texts: list[str], eval_texts: list[str]) -> list[float]:
    """Highest cosine similarity of each text to any evaluation question."""
    a = np.array([embeddings.embed_query(t) for t in texts])
    b = np.array([embeddings.embed_query(t) for t in eval_texts])
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    return (a @ b.T).max(axis=1).tolist()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--eval-questions", default="/eval/questions.yaml")
    p.add_argument("--out", required=True)
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--seeds", type=int, help="use only this many seed chunks (a pilot run)")
    p.add_argument("--per-seed", type=int, default=6)
    p.add_argument("--removed-fraction", type=float, default=0.15,
                   help="share of seed questions asked with the seed left out of the context")
    p.add_argument("--negatives", type=int, default=90)
    p.add_argument("--max-similarity", type=float, default=0.85,
                   help="drop questions this similar to an evaluation question")
    p.add_argument("--val-fraction", type=float, default=0.1)
    p.add_argument("--min-chars", type=int, default=300)
    p.add_argument("--k", type=int, default=4)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument("--extend", action="store_true",
                   help="keep the examples already in --out and only add --negatives new "
                        "unanswerable ones (to finish a run that stopped)")
    p.add_argument("--negatives-file",
                   help="unanswerable questions with their answers (data/negatives.yaml) instead "
                        "of asking the API for them")
    args = p.parse_args()

    rng = random.Random(args.random_seed)
    eval_questions = yaml.safe_load(open(args.eval_questions))["questions"]
    chunks = load_chunks(settings.corpus_dir)
    seeds = seed_chunks(chunks, eval_questions, args.min_chars)
    rng.shuffle(seeds)
    negatives = args.negatives
    if args.seeds:  # a pilot keeps the full run's proportions
        negatives = max(2, args.negatives * args.seeds // len(seeds))
        seeds = seeds[:args.seeds]
    print(f"{len(chunks)} chunks, {len(seeds)} seeds, {negatives} negatives", file=sys.stderr)

    out = Path(args.out)
    existing = []
    if args.extend:
        for name in ("train", "validation"):
            existing += [{**json.loads(line), "_split": name} for line in open(out / f"{name}.jsonl")]
        print(f"extending {len(existing)} existing examples", file=sys.stderr)

    usage: dict = {}
    headers = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"}
    with httpx.Client(headers=headers, timeout=120) as http, \
            concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        # 1-2. Questions.
        def seed_questions(i_seed):
            i, seed = i_seed
            out = ask_claude(http, args.model, QUESTIONS_PROMPT.format(
                source=seed.source, heading=seed.heading, text=seed.text,
                n=args.per_seed, half=args.per_seed // 2), QUESTIONS_SCHEMA, 1500, usage)
            return [{"seed": i, "lang": q["lang"], "question": q["question"].strip()}
                    for q in out["questions"]]

        items = [] if args.extend else [q for qs in pool.map(seed_questions, enumerate(seeds)) for q in qs]
        for q in items:
            q["remove_seed"] = rng.random() < args.removed_fraction
        if args.negatives_file:
            neg = yaml.safe_load(open(args.negatives_file))["questions"]
        else:
            neg = ask_claude(http, args.model, NEGATIVES_PROMPT.format(n=negatives, half=negatives // 2),
                             QUESTIONS_SCHEMA, 4000, usage)["questions"]
        asked = {norm(e["question"]) for e in existing}
        items += [{"seed": None, "lang": q["lang"], "question": q["question"].strip(), "remove_seed": False,
                   **({"answer": q["answer"].strip()} if "answer" in q else {})}
                  for q in neg if norm(q["question"]) not in asked]

        # 3. Contamination.
        sims = eval_similarity([q["question"] for q in items], [q["question"] for q in eval_questions])
        for q, s in zip(items, sims):
            q["eval_similarity"] = round(s, 3)
        dropped = [q for q in items if q["eval_similarity"] > args.max_similarity]
        items = [q for q in items if q["eval_similarity"] <= args.max_similarity]

        # 4. Retrieve like /ask, then the teacher answers that exact prompt.
        index = build_index(chunks)

        # Retrieval runs here, sequentially (the in-memory Qdrant isn't
        # thread-safe); only the teacher calls run in parallel.
        def retrieve(q):
            hits = index.query_points(COLLECTION, query=embeddings.embed_query(q["question"]),
                                      limit=args.k + 6, with_payload=True).points
            context = [Chunk(h.payload["source"], h.payload["heading"], h.payload["text"]) for h in hits]
            if q["seed"] is not None:
                seed = seeds[q["seed"]]
                q["seed_retrieved"] = seed in context[:args.k]
                if q["remove_seed"]:
                    # Its whole section: overlapping windows repeat the fact.
                    context = [c for c in context if (c.source, c.heading) != (seed.source, seed.heading)]
                elif not q["seed_retrieved"]:
                    # The fact is meant to be in context; where retrieval
                    # missed it, it takes the last place.
                    context = context[:args.k - 1] + [seed]
            q["context"] = context[:args.k]
            return q

        def answer(q):
            context = q.pop("context")
            messages = build_messages(q["question"], context)
            if "answer" in q:  # written by hand: always a decline
                out = {"answer": q.pop("answer"), "answerable": False}
            else:
                out = ask_claude(http, args.model, TEACHER_PROMPT.format(prompt=messages[0]["content"],
                                                                         language=LANGUAGES[q["lang"]]),
                                 ANSWER_SCHEMA, 800, usage)
            return {**q, "sources": [f"{c.source} - {c.heading}" for c in context],
                    "answerable": bool(out["answerable"]), "answer": out["answer"].strip(),
                    "messages": messages + [{"role": "assistant", "content": out["answer"].strip()}]}

        examples, errors, stopped = [], 0, None
        futures = [pool.submit(answer, retrieve(q)) for q in items]
        for f in concurrent.futures.as_completed(futures):
            try:
                examples.append(f.result())
            except OutOfCredit as e:
                # Keep what's done; --extend finishes it once there's credit.
                stopped = str(e)
                for other in futures:
                    other.cancel()
                break
            except Exception as e:  # one bad reply shouldn't cost the whole run
                errors += 1
                print(f"teacher error: {e}", file=sys.stderr)

    # Teacher replies that would teach the wrong thing: a seed question it
    # can't answer with its seed in context (a bad question), an off-topic
    # question it "answered", or an answer in the wrong language.
    bad = []
    for e in examples:
        if e["seed"] is not None and not e["remove_seed"] and not e["answerable"]:
            e["dropped_because"] = "seed question not answerable"
        elif e["seed"] is None and e["answerable"]:
            e["dropped_because"] = "negative answered"
        elif detected_language(e["answer"]) != e["lang"]:
            e["dropped_because"] = "wrong language"
        else:
            continue
        bad.append(e)
    examples = existing + [e for e in examples if "dropped_because" not in e]
    examples.sort(key=lambda e: (e["seed"] is None, e["seed"] or 0, e["question"]))
    for n, e in enumerate(examples):
        e["id"] = f"ex{n:04d}"

    # 5. Split by seed; negatives spread over both.
    val_seeds = set(range(len(seeds))[:max(1, round(len(seeds) * args.val_fraction))])
    split = {"train": [], "validation": []}
    for e in examples:
        if "_split" in e:
            split[e.pop("_split")].append(e)
            continue
        to_val = e["seed"] in val_seeds if e["seed"] is not None else rng.random() < args.val_fraction
        split["validation" if to_val else "train"].append(e)

    out.mkdir(parents=True, exist_ok=True)
    for name, rows in split.items():
        with open(out / f"{name}.jsonl", "w") as f:
            for e in rows:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    if args.extend:
        previous = json.load(open(out / "dropped.json"))
        dropped = previous["too_similar_to_eval"] + dropped
        bad = previous["bad_teacher_replies"] + bad
    with open(out / "dropped.json", "w") as f:
        json.dump({"too_similar_to_eval": dropped, "bad_teacher_replies": bad}, f, ensure_ascii=False, indent=2)

    def count(rows, **kw):
        return sum(all(e.get(k) == v for k, v in kw.items()) for e in rows)
    stats = {
        "model": args.model, "seeds": len(seeds), "teacher_errors": errors,
        "dropped_similar": len(dropped), "dropped_teacher": dict(collections.Counter(e["dropped_because"] for e in bad)),
        "max_eval_similarity_kept": max((e["eval_similarity"] for e in examples), default=0),
        "seed_retrieved": f"{count(examples, seed_retrieved=True)}/{sum('seed_retrieved' in e for e in examples)}",
        **{name: {"n": len(rows),
                  "answer_en": count(rows, answerable=True, lang="en"),
                  "answer_es": count(rows, answerable=True, lang="es"),
                  "decline_en": count(rows, answerable=False, lang="en"),
                  "decline_es": count(rows, answerable=False, lang="es")}
           for name, rows in split.items()},
        "usage": usage,
    }
    if args.extend:
        previous = json.load(open(out / "stats.json"))
        stats["teacher_errors"] += previous["teacher_errors"]
        stats["usage"] = {k: usage.get(k, 0) + previous["usage"].get(k, 0)
                          for k in usage.keys() | previous["usage"].keys()}
    if stopped:
        stats["stopped"] = stopped
    with open(out / "stats.json", "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
