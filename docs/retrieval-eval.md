# Retrieval evaluation

How well does the RAG service find the passage that answers a question? The
fourth EKS run answered 4 of 6 hand-picked questions correctly, missed one
fact that was in the docs, and showed that Spanish questions retrieved worse.
That was a spot check. This is the measurement that replaced it, and the CI
gate that keeps it from regressing.

## Method

`python -m rag.evaluate` (`apps/rag/rag/evaluate.py`) indexes the image's own
corpus (README, CLAUDE.md, docs/) into an in-memory Qdrant with the same
chunking and embeddings as the ingestion Job, then searches every question in
`apps/rag/eval/questions.yaml`:

- **30 answerable questions** (25 English, 5 Spanish), each with `evidence`:
  text that must appear in a retrieved chunk for it to count as a hit.
  Evidence is matched against chunk text rather than chunk ids, so changing
  the chunking doesn't invalidate the set; if a doc edit removes an evidence
  string, the evaluator fails and names the question to update.
- **5 unanswerable questions** (capital of France, today's date, ...), scored
  on similarity only.

Metrics, at `k=4` (the chunks the model gets as context):

| Metric | Meaning |
|---|---|
| hit@1 | the evidence is in the first result |
| hit@k | it's somewhere in the top 4 |
| MRR | mean of 1/rank of the first hit (0 if missed): rewards ranking it high |
| separation | lowest score of a hit minus highest score of an unanswerable question; positive means a similarity threshold could tell "nothing relevant" apart from a real match |

No model is called, so it's free and deterministic: two runs of the same
image give the same numbers. It retrieves only; whether the model then
answers well is a different question (LLM-as-judge, not done yet).

## Results

All on the same 35 questions and corpus, CHUNK_SIZE 1000 unless noted.

| Embedding model | hit@1 | hit@4 | MRR | Spanish hit@4 | Separation |
|---|---|---|---|---|---|
| BAAI/bge-small-en-v1.5 (before) | 0.50 | 0.70 | 0.59 | 0.40 | -0.069 |
| bge-small, CHUNK_SIZE 500 | worse | | | | |
| bge-small, its query instruction | same as plain | | | | |
| paraphrase-multilingual-mpnet-base-v2 | worse | | | | |
| potion-multilingual-128m | worse | | | | |
| google/embeddinggemma-300m, no prompts | 0.53 | 0.87 | 0.67 | 1.00 | -0.040 |
| **embeddinggemma-300m with task prompts (now)** | **0.57** | **0.90** | **0.69** | **0.80** | **+0.010** |

embeddinggemma is trained with task prompts, prepended here as
`QUERY_PREFIX` (`task: search result | query: `) and `DOCUMENT_PREFIX`
(`title: none | text: `). They improved English (hit@4 0.84 -> 0.92) at the
cost of one Spanish question (1.00 -> 0.80).

Caveats: Spanish has only 5 questions, so each one moves its hit@4 by 0.2,
and the English difference between the two embeddinggemma variants is two
questions. The set is small and was written by the same person who wrote the
docs; the numbers rank configurations, they aren't a general quality claim.

## Cost of the better model

Measured in the image, ONNX on CPU:

| | bge-small-en-v1.5 | embeddinggemma-300m |
|---|---|---|
| API memory, resident | ~350 MiB | ~850 MiB |
| Ingestion peak (2 CPUs, EMBED_BATCH 8) | 452 MiB | ~1 GiB |
| Query embedding, p50 at 1 CPU | 8 ms | 47 ms |
| Model load | 0.9 s | 3.1 s |
| Image, uncompressed / pulled | ~0.65 GB / ~0.25 GB | ~1.7 GB / ~0.7 GB |
| License | MIT | Gemma terms of use |

The latency doesn't matter next to 2-6 s of generation. The memory does: the
API and the ingestion Job went from a 768 MiB to a 1.5 GiB limit (1 GiB
requested), on platform nodes with 8 GiB. The pull is ~0.45 GB larger the
first time a node runs the image.

The trade-off was taken for multilingual retrieval (Spanish hit@4 0.40 ->
0.80) and +6 answerable questions in the top 4 (21 -> 27 of 30).

## Similarity threshold: not yet

The idea from the fourth run was to skip the model call when the best chunk
scores too low. With bge-small, an unanswerable question outscored a real hit
(separation -0.069): any threshold drops right answers. With embeddinggemma
the gap is +0.010 (best unanswerable 0.256, weakest hit 0.266): technically
separable, but too thin to trust on 5 unanswerable questions, and it didn't
survive adding this document to the corpus (-0.043: a weak hit's best chunk
is now this page). The model's own
"say you don't know" instruction handles them for now; a threshold needs more
negative questions first.

## In CI

The `rag-image` job runs the evaluation on the image it just built (its
model, chunking and docs), before scanning and publishing it, and fails if
any metric drops below `apps/rag/eval/baseline.json`:

| Minimum | Measured |
|---|---|
| all hit@k 0.85 | 0.90 |
| all MRR 0.63 | 0.672 |
| English hit@k 0.88 | 0.92 |
| Spanish hit@k 0.60 | 0.80 |

Measured with this document in the corpus: adding it moved MRR from 0.694
to 0.672 (it now outranks the source passage for a few questions about the
RAG service) without changing hit@k. Each minimum lets one question slip: the corpus is the docs themselves, so
every doc change re-ranks a little. Raise them when retrieval improves. Going
back to bge-small would fail all four. The full per-question table
goes to the job summary. It takes ~40 s on a laptop's 2 CPUs.

Locally, with the image built (`docker build -f apps/rag/Dockerfile -t
llm-platform-rag:dev .`):

```bash
docker run --rm -v "$PWD/apps/rag/eval":/eval:ro llm-platform-rag:dev \
  python -m rag.evaluate --questions /eval/questions.yaml --baseline /eval/baseline.json
```

`EMBEDDING_MODEL`, `QUERY_PREFIX`, `DOCUMENT_PREFIX`, `CHUNK_SIZE` and
`CHUNK_OVERLAP` are environment variables, so a candidate configuration can
be evaluated without code changes (a model other than the baked one needs
its cache mounted at `/models`, or `HF_HUB_OFFLINE=0` to download it).

## Answer quality: Claude as judge

Retrieval only says the right passage reached the model. Whether the answer
is right is graded by a second model, in two steps, because the model being
judged (Mistral-7B on vLLM) only exists while the EKS cluster is up:

1. **Generate**, on a cluster: `rag.answers` runs inside the rag-api pod,
   asks every question through `/ask` with `include_context` and writes each
   answer with the passages it was given and the model that produced it
   (answers from `claude-fallback`, while vLLM cold-starts, are labeled as
   such). The file is committed under `apps/rag/eval/answers/`.

   ```bash
   kubectl -n llm-platform exec -i deploy/rag-api -- python -m rag.answers \
     < apps/rag/eval/questions.yaml > apps/rag/eval/answers/<date>.json
   ```

2. **Judge**, anywhere: promptfoo (`apps/rag/eval/judge/`) grades the file
   with `claude-sonnet-5`, locally in Docker or with the manual
   *Judge RAG answers* workflow (`judge.yml`, `ANTHROPIC_API_KEY` secret).
   Not on every push: it costs money and isn't deterministic.

Checks, each pass/fail with the judge's reason:

| Check | Questions | Passes when |
|---|---|---|
| correct | answerable | it agrees with the question's `reference` answer |
| faithful | answerable | every claim is in the passages the model was given |
| declines | unanswerable | it says it doesn't know instead of answering from general knowledge |
| language | all | it's in the question's language |

`summary.py` reports the pass rate per check by language and by answering
model, and lists every failure with the judge's reason.

**Calibrating the judge.** `judge/calibration.json` holds five hand-written
answers with known verdicts: a correct one, one that adds an invented
mechanism, one with a wrong number, an English answer to a Spanish question,
and a correct refusal. A judge that disagrees with any of the 14 expected
verdicts fails the summary. promptfoo's built-in `context-faithfulness`
failed this: it splits an answer into statements and averages them, and the
correct 2-sentence answer scored between 0.33 and 0.75 across runs (threshold
0.8), with no reason given. A pass/fail rubric that has to name the
unsupported claim agreed on 14 of 14, on two consecutive runs. A calibration
run is 10 judge calls, ~12k tokens.
