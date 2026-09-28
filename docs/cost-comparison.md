# Cost comparison — local vLLM vs. external provider

> Template to fill in with real data once the project is deployed and you've
> run test traffic through the gateway (LiteLLM + Prometheus/Grafana).

## Cost per 1,000 requests

| Path | Model | Infra cost | Cost per 1,000 req | p50 latency | p95 latency |
|------|--------|------------------------|----------------------|---------------|---------------|
| Local (vLLM on g4dn.xlarge spot) | Quantized Mistral-7B | $ (compute: spot price/hour ÷ requests/hour) | $ | ms | ms |
| External provider via gateway | claude-sonnet-5 (example) | $0 own infra | $ (per provider pricing) | ms | ms |

## How to fill in this table

1. **GPU node infrastructure cost:** take the current spot price of your
   instance (check the AWS calculator at the time you run the project)
   and divide it by the measured real throughput (requests/hour your vLLM
   sustains under the current config).
2. **External provider cost:** the provider's public pricing (price per
   million input/output tokens) multiplied by the average tokens per
   request for your use case.
3. **Latencies:** pull them from the "LLM Platform: Cost and Performance"
   Grafana dashboard (`k8s/observability/grafana-dashboard-llm-cost.yaml`).
   It also computes items 1 and 2 live: set the API prices and the GPU node
   hourly cost in its variables, and select `vllm-inference` as the backend.

## Conclusion (to write at the end)

Space for your analysis: at what traffic volume does it make sense to keep
the local model running 24/7 vs. scaling to 0 with KEDA and using the
external provider as fallback? This is the central question an "AI Platform
Engineer" answers in a real business case.
