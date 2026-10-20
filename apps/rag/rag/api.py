"""RAG API: question -> retrieve from Qdrant -> answer through LiteLLM.

    POST /ask      {"question": "...", "top_k": 4}
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
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel, Field

from . import embeddings, store
from .config import settings
from .prompt import build_messages

REQUESTS = Counter("rag_requests_total", "RAG /ask requests by outcome", ["outcome"])
STAGE_SECONDS = Histogram("rag_stage_seconds", "Time per /ask stage", ["stage"],
                          buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))


@asynccontextmanager
async def lifespan(app: FastAPI):
    embeddings.dimension()  # load the model before taking traffic
    app.state.qdrant = store.client()
    app.state.http = httpx.Client(timeout=settings.llm_timeout)
    yield
    app.state.http.close()


app = FastAPI(title="llm-platform RAG", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=10)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/ask")
def ask(body: Ask) -> dict:
    t = time.perf_counter()
    vector = embeddings.embed([body.question])[0]
    STAGE_SECONDS.labels("embed").observe(time.perf_counter() - t)

    t = time.perf_counter()
    try:
        hits = store.search(app.state.qdrant, vector, body.top_k)
    except Exception as e:  # collection missing until the first ingestion
        REQUESTS.labels("retrieval_error").inc()
        raise HTTPException(503, f"retrieval failed: {e}") from e
    STAGE_SECONDS.labels("retrieve").observe(time.perf_counter() - t)

    t = time.perf_counter()
    try:
        resp = app.state.http.post(
            f"{settings.litellm_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
            json={"model": settings.chat_model, "temperature": 0, "max_tokens": 400,
                  "messages": build_messages(body.question, [chunk for chunk, _ in hits])},
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        REQUESTS.labels("llm_error").inc()
        raise HTTPException(502, f"model call failed: {e}") from e
    STAGE_SECONDS.labels("generate").observe(time.perf_counter() - t)

    data = resp.json()
    REQUESTS.labels("ok").inc()
    return {
        "answer": data["choices"][0]["message"]["content"],
        # Which model actually answered: local, or claude-fallback.
        "model": data.get("model"),
        "sources": [{"n": n, "source": c.source, "heading": c.heading, "score": round(score, 3)}
                    for n, (c, score) in enumerate(hits, start=1)],
    }
