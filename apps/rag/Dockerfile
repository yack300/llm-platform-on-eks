# RAG ingestion + API (apps/rag/). Built and signed by CI, run by k8s/rag/.
# Build from the repo root, which the corpus (the project's docs) comes from:
#   docker build -f apps/rag/Dockerfile -t llm-platform-rag:dev .
#
# requirements.txt is the full lock, regenerated after editing requirements.in:
#   docker run --rm -v "$PWD/apps/rag":/w -w /w python:3.12.15-slim sh -c \
#     'pip install -q -r requirements.in && pip freeze > requirements.txt'

# Same base in both stages, pinned by digest.
ARG PYTHON=python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

FROM ${PYTHON} AS build
# Must match EMBEDDING_MODEL in rag/config.py.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
COPY apps/rag/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --prefix=/install -r /tmp/requirements.txt
# Bake the embedding model in, so pods never download it at startup.
# The Hugging Face cache writes some files owner-only; the app runs as 10001.
RUN PYTHONPATH=/install/lib/python3.12/site-packages python -c \
    "from fastembed import TextEmbedding; TextEmbedding('${EMBEDDING_MODEL}', cache_dir='/models')" \
 && chmod -R a+rX /models

FROM ${PYTHON}
LABEL org.opencontainers.image.source=https://github.com/yack300/llm-platform-on-eks \
      org.opencontainers.image.description="RAG ingestion and API for llm-platform-on-eks"
COPY --from=build /install /usr/local
COPY --from=build /models /models
COPY apps/rag/rag /app/rag
# The corpus: the project's own documentation, versioned with the image.
COPY README.md CLAUDE.md /corpus/
COPY docs /corpus/docs
WORKDIR /app
# HOME on /tmp: the only writable path with readOnlyRootFilesystem.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 HOME=/tmp \
    MODEL_CACHE=/models CORPUS_DIR=/corpus
# Non-root, numeric so Kubernetes can verify runAsNonRoot.
USER 10001
EXPOSE 8080
CMD ["uvicorn", "rag.api:app", "--host", "0.0.0.0", "--port", "8080"]
