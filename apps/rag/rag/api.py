"""RAG API: question -> retrieve from Qdrant -> answer through LiteLLM.

    POST /ask      {"question": "...", "top_k": 4, "include_context": false}
    GET  /healthz  liveness/readiness
    GET  /metrics  Prometheus (scraped via k8s/observability/servicemonitors.yaml)

The model call goes through the LiteLLM gateway like any other client, so
it gets the same routing (vLLM, or claude-fallback while vLLM cold-starts),
metrics and cost tracking. Endpoints are plain `def`: FastAPI runs them in
its threadpool, which suits the CPU-bound embedding and blocking HTTP calls.
"""
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry import propagate, trace
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel, Field

from . import embeddings, store, tracing
from .config import settings
from .prompt import build_messages

REQUESTS = Counter("rag_requests_total", "RAG /ask requests by outcome", ["outcome"])
STAGE_SECONDS = Histogram("rag_stage_seconds", "Time per /ask stage", ["stage"],
                          buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
tracer = trace.get_tracer("rag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.setup()
    embeddings.dimension()  # load the model before taking traffic
    app.state.qdrant = store.client()
    app.state.http = httpx.Client(timeout=settings.llm_timeout)
    yield
    app.state.http.close()
    tracing.shutdown()


def _untraced(scope) -> bool:
    # Kubelet probes and Prometheus scrapes would be a trace every few seconds.
    return scope["path"] == "/healthz" or scope["path"].startswith("/metrics")


# FastAPI's native telemetry traces each request once tracing.setup() has
# installed a provider: the server span continues a caller's traceparent and
# is the parent of the rag.* spans. Operation spans (dependencies,
# serialization) add nothing here.
app = FastAPI(title="llm-platform RAG", lifespan=lifespan,
              telemetry={"exclude": _untraced, "operation_spans": False})
app.mount("/metrics", make_asgi_app())


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=10)
    # Return each passage's text with its source: what an answer-quality
    # judge needs to check the answer against (rag.answers sets it).
    include_context: bool = False


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/ask")
def ask(body: Ask) -> dict:
    # One trace per question: rag.ask under FastAPI's request span, and
    # LiteLLM's spans under rag.generate (it reads the traceparent injected
    # into the model call), so Tempo shows the whole path.
    with tracer.start_as_current_span("rag.ask") as span:
        # The question is the project's docs domain, not user data worth
        # redacting; it's what makes a trace readable.
        span.set_attribute("rag.question", body.question[:500])
        span.set_attribute("rag.top_k", body.top_k)

        with tracer.start_as_current_span("rag.embed"):
            t = time.perf_counter()
            vector = embeddings.embed_query(body.question)
            STAGE_SECONDS.labels("embed").observe(time.perf_counter() - t)

        with tracer.start_as_current_span("rag.retrieve") as retrieve:
            t = time.perf_counter()
            try:
                hits = store.search(app.state.qdrant, vector, body.top_k)
            except Exception as e:  # collection missing until the first ingestion
                REQUESTS.labels("retrieval_error").inc()
                raise HTTPException(503, f"retrieval failed: {e}") from e
            STAGE_SECONDS.labels("retrieve").observe(time.perf_counter() - t)
            retrieve.set_attribute("rag.top_score", round(hits[0][1], 3) if hits else 0.0)
            retrieve.set_attribute("rag.sources", [f"{c.source} - {c.heading}" for c, _ in hits])

        with tracer.start_as_current_span("rag.generate") as generate:
            generate.set_attribute("gen_ai.operation.name", "chat")
            generate.set_attribute("gen_ai.request.model", settings.chat_model)
            headers = {"Authorization": f"Bearer {settings.litellm_api_key}"}
            propagate.inject(headers)  # traceparent for LiteLLM
            t = time.perf_counter()
            try:
                resp = app.state.http.post(
                    f"{settings.litellm_url}/chat/completions",
                    headers=headers,
                    json={"model": settings.chat_model, "temperature": 0, "max_tokens": 400,
                          "messages": build_messages(body.question, [chunk for chunk, _ in hits])},
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                REQUESTS.labels("llm_error").inc()
                raise HTTPException(502, f"model call failed: {e}") from e
            STAGE_SECONDS.labels("generate").observe(time.perf_counter() - t)
            data = resp.json()
            usage = data.get("usage") or {}
            generate.set_attribute("gen_ai.response.model", data.get("model") or "")
            generate.set_attribute("gen_ai.usage.input_tokens", usage.get("prompt_tokens", 0))
            generate.set_attribute("gen_ai.usage.output_tokens", usage.get("completion_tokens", 0))

        REQUESTS.labels("ok").inc()
        sources = []
        for n, (c, score) in enumerate(hits, start=1):
            source = {"n": n, "source": c.source, "heading": c.heading, "score": round(score, 3)}
            if body.include_context:
                source["text"] = c.text
            sources.append(source)
        return {
            "answer": data["choices"][0]["message"]["content"],
            # Which model actually answered: local, or claude-fallback.
            "model": data.get("model"),
            "sources": sources,
        }
