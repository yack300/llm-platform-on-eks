# Fine-tuning a behavior LoRA for the RAG model

The judge measured one failure no prompt fixed: Mistral-7B answered **0 of 7**
Spanish questions in Spanish, in four runs, whatever the prompt said
([retrieval-eval.md](retrieval-eval.md#first-judged-run-on-eks-2026-10-26)).
Qwen2.5-7B did better at that but answered off-topic questions from general
knowledge, so it wasn't adopted. This is the other option: keep Mistral and
teach it the behavior with a LoRA adapter, trained and evaluated on the same
platform.

## The pipeline

| Step | Where | What |
|---|---|---|
| Dataset | `apps/finetune/data/` | 247 examples: the exact `/ask` prompt (real retrieval, top 4) and a reference answer, half English, half Spanish; 102 of them decline |
| Train | Argo Workflows on a GPU node | QLoRA (NF4 base, fp16, r=16 on every projection) of `mistralai/Mistral-7B-Instruct-v0.2`; loss on the answer only |
| Register | MLflow | parameters, losses, the adapter (in S3) and a new version of `rag-behavior-lora`, alias `candidate` |
| Evaluate | same workflow | base vs adapter on the 28 validation examples, scored by `finetune/behavior.py` (no API) |

The dataset's questions come from passages that hold none of the retrieval
evaluation's evidence or questions, so the evaluation set stays unseen; the
validation split comes from passages training never asked about. Reference
answers were written by Claude for real retrievals, with the question's
language stated explicitly: without it, a pilot answered 15 of 16 English
questions in Spanish. The API account ran out of credit after the answerable
questions; the 90 unanswerable ones were written by hand
(`data/negatives.yaml`).

Everything runs from Git: Argo CD deploys Argo Workflows, MLflow and the
`finetune` WorkflowTemplate; the trainer and MLflow images are built, scanned
and signed by CI, and Kyverno admits them only with that signature. MLflow
reaches the artifacts bucket (in `terraform/bootstrap`, so adapters outlive
the cluster) through EKS Pod Identity, with no credentials in the cluster.

## Eighth EKS run (2026-11-17): the first adapter

One workflow, on a g4dn.xlarge (T4) that Karpenter launched for it, on
demand (no spot capacity):

| | |
|---|---|
| Whole workflow | 48 min: train step 37 min (node, ~14 GB of weights, training), evaluation 11 min |
| Training, 2 epochs, 219 examples | 29 min (1,761 s) |
| Trainable parameters | 42 M of 7.3 B (0.6%) |
| Adapter | 160 MiB (`adapter_model.safetensors`) |
| Train loss | 1.60 at the start, ~0.25-0.34 at the end |
| Validation loss | 0.664 after epoch 1, 0.682 after epoch 2 |

**Behavior on the 28 validation examples** (heuristics: stopwords for the
language, phrases for a refusal, `[n]` for a citation):

| | Mistral-7B | + LoRA |
|---|---|---|
| Answer in the question's language | 14/28 | **28/28** |
| Spanish questions answered in Spanish | **0/14** | **14/14** |
| Unanswerable questions declined | 9/12 | 10/12 |
| Answerable questions refused | 0/16 | 0/16 |
| Answers that cite a passage | 15/16 | 16/16 |
| Average answer length | 489 characters | 250 |

The same heuristics reproduce the judge's language result on past answer
files (Mistral 0/7, Qwen 4/7 Spanish in Spanish), so the language row can be
trusted. The others can't tell a right answer from a wrong one.

**What reading every answer shows.** The adapter fixes the language and the
form of a refusal: off-topic questions get one sentence in the right
language ("No lo sé; la documentación no trata ese tema."), where the base
model rambled and once answered "why is the sky blue?" with a paragraph on
LiteLLM's retries. It doesn't make answers more faithful, and it's more
assertive at the edges:

- Both near-miss questions (the passage with the answer removed from the
  context) were answered anyway, from neighboring passages: the second run's
  image pull time given for the third run.
- Of the 16 answerable questions, about 9 are right, 2 incomplete and 5
  state an invented or mixed-up fact ("only 1 of 7 answered correctly",
  "vLLM (a LLM model)"). That's my reading, not a measurement.

The rising validation loss in epoch 2 and the near misses point the same
way: one epoch and more examples whose right answer is to decline when the
passage is close but not there. Nothing gets served without the judge's
`correct` and `faithful` checks on the live RAG service, which need API
credit; the adapter waits in S3 as version 1, alias `candidate`.

**Found on the way:**

- **Argo CD's application controller OOMKilled in a loop** at its 1 GiB
  limit once Argo Workflows was installed: its CRDs with full schemas are
  ~5 MB each (~20 MB for four) and the controller caches them. Nothing
  synced past wave 0 for ~20 minutes; the limit is now 2 GiB. A StatefulSet
  doesn't replace a crash-looping pod on update: it had to be deleted.
- **MLflow 3** rejects unknown Host headers (`--allowed-hosts`) and starts a
  background job runner worth ~2 GiB (`MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`);
  the official image can't write to S3 (no boto3).
- **CUDA 13 on the T4:** torch 2.15 from PyPI is built for CUDA 13; the
  vLLM image the GPU nodes already ran is too, which showed the Bottlerocket
  driver supports it. CI checks the build includes `sm_75` before
  publishing.
- Kyverno pins each signed image to its digest in the Deployment, so Argo CD
  shows `rag` and `training` as OutOfSync while Healthy; cosmetic.

Full results, including every generation: `apps/finetune/results/2026-11-17-mistral-lora-2ep.json`.
