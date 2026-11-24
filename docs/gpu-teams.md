# Two teams, two GPUs: queues, quotas and who pays

The platform has two kinds of GPU work. Serving (vLLM behind the RAG
service) has users waiting on it. Training (the fine-tuning workflows in
[fine-tuning.md](fine-tuning.md)) can wait, and can start over if it has to.
Until now they shared the GPU quota by luck: the fine-tuning runs trained
first, while vLLM was scaled to zero. This page turns that into rules:
each team gets a quota, idle quota can be borrowed, and the team that owns
it gets it back.

## The teams

| Team | Namespace | Runs | Its GPU | Rule |
|---|---|---|---|---|
| product | `llm-platform` | vLLM (the RAG model) | the on-demand one | never interrupted: not by AWS (on-demand), not by research (it reclaims) |
| research | `training` | fine-tuning workflows | the spot one | can borrow product's idle GPU share; evicted when product needs it |

The account's GPU quota allows one g4dn.xlarge spot and one on-demand, so
each team's quota is one T4. With time-slicing (phase C) each T4 is
advertised as 2 shares, and quotas count shares.

## How it works

- **Kueue** (`k8s/teams/`, Argo CD app `kueue`, wave 0) admits GPU pods
  against the quotas. Each team has a ClusterQueue in the same cohort
  (`gpus`), so unused quota can be lent, and a LocalQueue in its namespace.
- **Flavors** map the quota to capacity types: `gpu-on-demand` and
  `gpu-spot`. Kueue adds the flavor's `karpenter.sh/capacity-type` to the
  admitted pod's node selector, and Karpenter launches that kind of node.
- **product**'s queue reclaims its own quota from a borrower
  (`reclaimWithinCohort: Any`); research's queue never preempts anyone.
- **What gets queued:** only pods with a `kueue.x-k8s.io/queue-name`
  label: vLLM's Deployment (`product`) and the fine-tuning workflow's pods
  (`research`, through the WorkflowTemplate's `podMetadata`). Kueue's
  Deployment integration copies the label to each pod. Everything else in
  those namespaces (LiteLLM, RAG, Qdrant, MLflow) is untouched.
- **Kyverno** (`require-gpu-queue`) denies a pod that requests
  `nvidia.com/gpu` in those namespaces without that label: it would take a
  GPU outside every quota.
- Quotas cover GPUs only (`excludeResourcePrefixes` for CPU and memory):
  the scheduler and Karpenter already handle those.

Two settings were chosen for safety rather than defaults:

- Kueue's pod webhook fails closed (`failurePolicy: Fail`), and the chart's
  default namespace selector is every namespace but `kube-system` and
  Kueue's own. With Kueue down, no pod could be created in any other
  namespace, Argo CD's included.
  `managedJobsNamespaceSelector` limits it to `llm-platform` and `training`.
- The chart requests 500m CPU for the controller, half of what the
  platform NodePool's 4-CPU limit leaves for add-ons: 100m here.

## Phase A: the rules on kind (2026-11-23)

`scripts/teams-local.sh` builds a kind cluster whose two workers advertise
a fake `nvidia.com/gpu: 2` (a status patch) and carry Karpenter's
capacity-type label, installs Kueue with the same values as EKS and Argo
Workflows, applies the same queues, and plays the scenario with pods that
only sleep:

| Step | What happened | Time |
|---|---|---|
| research starts 3 GPU steps (one workflow) | 2 admitted on `gpu-spot`, 1 borrowing `gpu-on-demand` | all 3 running in 17 s (image pulls included) |
| product scales its vLLM stand-in to 2 replicas | Kueue evicts the borrowing step; product's 2 pods run | 2 s |
| | the evicted step waits, gated, in research's queue (1 pending) | |
| product scales to 0 (what KEDA does with no demand) | the waiting step is admitted on `gpu-on-demand` again | 5 s |
| product at 1 replica | the borrowing step keeps running: product needs 1 of its 2 shares | |

**What eviction looks like for a workflow:** Kueue deletes the pod, and
Argo Workflows creates it again under the same name, in the same workflow
step, within about 2 seconds; no retry is used. The new pod is gated until
Kueue admits it, then the step starts over. For a 30-minute training step,
an eviction throws away whatever it had done.

**Teardown:** Kueue puts finalizers on the pods and Workloads it queues,
and only Kueue removes them. If its Application were deleted before them
in the teardown's cascade, they'd block their namespaces' deletion, so
`scripts/teardown-eks.sh` now empties the queues first (vLLM's app, then
the workflows) and waits for the Workloads to go: 2 s on kind.

Also confirmed: the Kyverno policy denies a GPU pod without a queue
(`tests/kyverno/`, run in CI), and Kueue's admission adds the flavor's
node selector (`karpenter.sh/capacity-type: on-demand` on vLLM's pods).

## Open questions for EKS (phase C)

- **Time-slicing** on Bottlerocket (`settings.kubelet-device-plugins.nvidia`,
  `time-slicing.replicas: 2`): there's no memory isolation. vLLM takes 85%
  of the T4's 16 GB today, and a QLoRA step needs ~10 GB, so they can't
  share a GPU as configured. Kueue counts shares, not memory.
- **Karpenter doesn't know about time-slicing:** it sizes a g4dn.xlarge as
  1 GPU, so two pending pods asking for one share each may make it launch
  two nodes where one would do, against a quota of one instance per
  capacity type.
- **Kueue and scale-to-zero:** a vLLM pod now waits for admission before
  Karpenter sees it. Locally that's a couple of seconds; on a cold start
  it adds to the ~5 minutes.
- **Chargeback (phase B):** how OpenCost splits a time-sliced GPU's cost
  between the pods sharing it isn't documented. To be measured against
  Cost Explorer.
