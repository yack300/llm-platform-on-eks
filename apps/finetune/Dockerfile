# LoRA fine-tuning (python -m finetune.train / finetune.evaluate), run by the
# Argo Workflows template in k8s/training/. Build from the repo root:
#   docker build -f apps/finetune/Dockerfile -t llm-platform-finetune:dev .
# The default is the CPU variant (the smoke test on kind); the GPU variant
# for EKS takes its own lock and torch index as build args.
#
# requirements-cpu.txt is the full lock, regenerated after editing
# requirements.in:
#   docker run --rm -v "$PWD/apps/finetune":/w -w /w python:3.12.16-slim sh -c \
#     'pip install -q --extra-index-url https://download.pytorch.org/whl/cpu \
#      -r requirements.in && pip freeze > requirements-cpu.txt'
# requirements-cuda.txt is resolved for linux x86_64 without installing (the
# CUDA wheels are ~3 GB): pip install --dry-run --report with
# --platform manylinux_2_<5..40>_x86_64 --python-version 3.12
# --only-binary=:all: -r requirements-cuda.in, then the report's name==version.
#
# GPU variant (CI publishes it from main):
#   docker build -f apps/finetune/Dockerfile --build-arg LOCK=requirements-cuda.txt \
#     --build-arg TORCH_INDEX=https://pypi.org/simple -t llm-platform-finetune:gpu .

# Same base as the RAG image, pinned by digest.
ARG PYTHON=python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

FROM ${PYTHON} AS build
ARG LOCK=requirements-cpu.txt
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
COPY apps/finetune/${LOCK} /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --prefix=/install \
      --extra-index-url ${TORCH_INDEX} -r /tmp/requirements.txt

FROM ${PYTHON}
# Debian security fixes not yet in the python image (as in the RAG image).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* \
 && mkdir /cache && chown 10001 /cache
LABEL org.opencontainers.image.source=https://github.com/yack300/llm-platform-on-eks \
      org.opencontainers.image.description="LoRA fine-tuning for llm-platform-on-eks"
COPY --from=build /install /usr/local
COPY apps/finetune/finetune /app/finetune
# The dataset is versioned with the image, so a run is reproducible from
# the image tag alone (its hash is logged to MLflow too).
COPY apps/finetune/data/dataset/train.jsonl apps/finetune/data/dataset/validation.jsonl /data/
COPY apps/finetune/VERSION /app/VERSION
WORKDIR /app
# Base model weights download to HF_HOME (an emptyDir in the workflow).
# GIT_PYTHON_REFRESH: MLflow looks for git to tag runs; there is none here.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp HF_HOME=/cache/huggingface \
    GIT_PYTHON_REFRESH=quiet
USER 10001
CMD ["python", "-m", "finetune.train", "--help"]
