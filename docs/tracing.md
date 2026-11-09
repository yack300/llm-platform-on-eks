# Tracing a RAG request end to end

Metrics said a RAG answer took a few seconds. They couldn't say where those
seconds went: embedding the question, searching Qdrant, the gateway, or the
model. Tracing answers that per request.

## What's traced

On EKS, the RAG API and LiteLLM send OpenTelemetry spans over OTLP/HTTP to
Grafana Tempo, which Grafana queries (Explore, Tempo datasource). One
question is one trace:

```
rag-api  POST /ask                          FastAPI's native request span
rag-api    rag.ask                          question, top_k
rag-api      rag.embed                      the question's embedding (CPU)
rag-api      rag.retrieve                   Qdrant search: top score, sources
rag-api      rag.generate                   model, input/output tokens
litellm        Received Proxy Server Request
litellm          auth, router, ...
litellm          litellm_request            one per attempt: model, tokens, status
```

LiteLLM's spans land in the same trace because the API injects a
`traceparent` header into the model call and LiteLLM's `otel` callback
continues it. Probes (`/healthz`) and Prometheus scrapes (`/metrics`) aren't
traced. Configuration: `apps/rag/rag/tracing.py`, `k8s/rag/api.yaml`,
`k8s/litellm/`, `k8s/argocd/apps/tempo.yaml`; details in CLAUDE.md.

Tracing is off when `OTEL_EXPORTER_OTLP_ENDPOINT` isn't set, so unit tests,
CI's retrieval evaluation and the local kind profile don't change. Tempo
runs as a single binary without a volume: traces last 24 hours or until the
pod restarts. LiteLLM's spans carry the full prompt, retrieved passages
included.

## Seventh EKS run (2026-11-08): where the time goes

Ten questions from the evaluation set, Mistral-7B warm, per-stage medians
from Tempo:

| Stage | p50 | Share |
|---|---|---|
| Whole request (`POST /ask`) | 2.93 s | 100% |
| `rag.embed` (embeddinggemma on CPU) | 172 ms | ~6% |
| `rag.retrieve` (Qdrant) | 6 ms | <1% |
| `rag.generate` (LiteLLM + Mistral) | 2.74 s | ~93% |
| LiteLLM's own overhead | ~5 ms | <1% |

With the model warm, the RAG service and the gateway add under 200 ms: making
answers faster is about the model, not retrieval.

**The cold-start fallback is mostly retries.** The same question asked while
vLLM was still scaled to zero took 8.57 s and was answered by
claude-fallback. The trace shows where:

| Part of LiteLLM's 8.05 s | Duration |
|---|---|
| The answer itself: `litellm_request` to claude-sonnet-5 | 1.77 s |
| Everything before it: the failed call to local-mistral (0.88 s, a 500 with vLLM down) and the router's retries before falling back | ~6.3 s |

About three quarters of that request went to retrying a backend that was
down (`num_retries: 2` with `fallbacks` in `router_settings`). Metrics only
showed a slow request; the trace shows the fix belongs in LiteLLM's retry
and fallback settings, not in the fallback model.

**The fix, measured locally.** The same situation reproduced with LiteLLM
v1.99.1 in Docker (local model's address refusing connections, fallback
answering), three requests in a row:

| `router_settings` | 1st | 2nd | 3rd |
|---|---|---|---|
| `num_retries: 2` (before) | 7.5 s | 5.5 s | 4.1 s |
| `num_retries: 0` (now, EKS config) | 0.87 s | 0.05 s | 0.04 s |
| `num_retries: 2` + `retry_policy: {InternalServerErrorRetries: 0}` | 5.7 s | 4.5 s | 4.3 s |

A per-error `retry_policy` doesn't help: a refused connection isn't an
internal server error to LiteLLM. With no retries, the first attempt still
goes to local-mistral and LiteLLM's `litellm_deployment_total_requests_total`
for it still counts every request (5 of 5 in the same test), which is what
KEDA's `gateway-demand` trigger reads to wake vLLM. The cost: a transient
vLLM error is answered by claude-fallback instead of being retried. Not yet
measured on EKS.

The first embedding after the API starts is slower (433 ms vs ~170 ms) while
ONNX Runtime warms up.

## Reading the traces

The traces above were sent with a caller-chosen `traceparent`, to look them up
by ID, so Grafana lists them with `<root span not yet received>`: their
parent lives in the test client, which doesn't export spans. Requests without
one have `POST /ask` as root. Query all RAG traces with TraceQL:
`{resource.service.name="rag-api"}`.
