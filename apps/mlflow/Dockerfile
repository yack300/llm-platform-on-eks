# MLflow tracking server (k8s/training/mlflow.yaml): the official image plus
# boto3, which it lacks, to proxy artifacts to S3 on EKS. Built, scanned and
# signed by CI like the other images, so the cluster's signature policy
# covers it. From the repo root:
#   docker build -f apps/mlflow/Dockerfile -t llm-platform-mlflow:dev .
FROM ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
LABEL org.opencontainers.image.source=https://github.com/yack300/llm-platform-on-eks \
      org.opencontainers.image.description="MLflow server with S3 support for llm-platform-on-eks"
COPY apps/mlflow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt && rm /tmp/requirements.txt
ENV HOME=/tmp
USER 10001
