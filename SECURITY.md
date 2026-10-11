# Security policy

## Reporting a vulnerability

Please don't open a public issue for security problems. Report them privately
through GitHub's [private vulnerability reporting](https://github.com/yack300/llm-platform-on-eks/security/advisories/new)
instead. I'll acknowledge the report as soon as I can and keep you updated
until it's resolved.

## Scope

This is a reference/portfolio platform, meant to be deployed short-lived and
torn down after use, not a production service. Reports are most useful for:

- Secrets or credentials committed to the repository.
- Terraform or Kubernetes configuration that exposes the cluster or the AWS
  account beyond what's documented (e.g. the EKS API endpoint is public but
  restricted to `admin_cidrs`).
- Weaknesses in the CI pipeline (GitHub Actions) that could let untrusted
  code reach `main`, which Argo CD deploys automatically.

Vulnerabilities in the third-party components themselves (vLLM, LiteLLM,
Qdrant, Argo CD, KEDA, etc.) should be reported to their maintainers.

## Supported versions

Only the `main` branch is maintained.
