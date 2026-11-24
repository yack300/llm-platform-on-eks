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

**What eviction looks like for a workflow:** Kueue deletes the pod and Argo
Workflows marks the step `Error: pod deleted`. The step's `retryStrategy`
(`OnError`) then creates a new pod, which waits in the queue and starts the
step over; for a 30-minute training step, an eviction throws away whatever
it had done. Without the retry the step stays failed and the workflow fails.

This page first said the opposite (commit `7d6105e`: Argo creates the pod
again by itself, no retry needed). That test evicted steps seconds after
they started, while Argo still had them as Pending, and a pod deleted then
*is* created again. A step that has been running isn't. It showed up in
phase B, as ten minutes of research's GPU time missing from the cost
report; `teams-local.sh` now lets the step run for a minute first (on that
run, product's replicas were ready in 11 s and the retried step in 8 s).

**Teardown:** Kueue puts finalizers on the pods and Workloads it queues,
and only Kueue removes them. If its Application were deleted before them
in the teardown's cascade, they'd block their namespaces' deletion, so
`scripts/teardown-eks.sh` now empties the queues first (vLLM's app, then
the workflows) and waits for the Workloads to go: 2 s on kind.

Also confirmed: the Kyverno policy denies a GPU pod without a queue
(`tests/kyverno/`, run in CI), and Kueue's admission adds the flavor's
node selector (`karpenter.sh/capacity-type: on-demand` on vLLM's pods).

## Phase B: who pays, on kind (2026-11-24)

OpenCost 1.121.4 (Argo CD app `opencost`) and `scripts/gpu-chargeback.py`,
tested with a scenario whose cost can be worked out by hand
(`scripts/chargeback-local.sh`): the fake T4s priced at $0.526/h on-demand
and $0.27/h spot, all of it on the GPU, and three phases of 10 minutes
(research borrows; product takes its GPU back; research borrows again).

The report's rules: a team owes the GPU's price for the fraction of its
shares it held, at that node's price, so a borrowed share is paid by the
borrower. Shares nobody held are owed by the team whose quota they are.

| | By hand, used | Report, used | Report, idle | Report, owed | OpenCost |
|---|---|---|---|---|---|
| product | $0.0877 | $0.0789 | $0.1052 | $0.1841 | $0.1578 |
| research | $0.2227 | $0.2094 | $0.0045 | $0.2139 | $0.4188 |
| both (nodes' real cost: $0.3980) | $0.3103 | $0.2883 | $0.1097 | **$0.3980** | **$0.5766** |

- **OpenCost bills a time-sliced share as a whole GPU.** Both teams are
  charged exactly twice what they used, 45% more than both nodes cost even
  with shares sitting idle. It multiplies the shares requested by the price
  of a GPU, and here also counts 2 GPUs on a node that has one.
- **GPU Feature Discovery's labels fix only half.** With
  `nvidia.com/gpu.count=1` and `.replicas=2` on the nodes, OpenCost counts
  1 GPU per node, and still charges pods double: the same scenario with and
  without the labels (two earlier runs) billed the same $0.4977 for nodes
  that cost $0.40. Its source has the correction (`pkg/costmodel/metrics.go`
  scales the allocation by GPUs/shares) behind
  `verr != nil && err != nil`, that is, only when reading both numbers
  *failed*; the condition looks inverted. Not reported upstream yet.
- **The report adds up to the real cost** because it takes only the GPU
  hours from OpenCost, and the nodes' uptime and price from Prometheus.
- **Short pods lose time.** A pod counts up to about a minute less than it
  ran (0.300 share-hours for 0.333), which the report hands to the owner as
  idle. With OpenCost's default 5-minute resolution it was far worse: in a
  6-minute test, product's pods and everything on the on-demand node were
  missing. `queryResolutionSeconds: 60` here.
- OpenCost's `/assets` API gave the nodes 0 minutes of life in that test;
  the report doesn't use it.

Not covered: on EKS a GPU node's price includes its CPU and memory, which
this charges to nobody, and spot prices need configuring in OpenCost. And
shares held aren't GPU time used: a team pays for what it reserves.

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
- **Chargeback against the bill:** the report's total compared with Cost
  Explorer's GPU instances, with the node's CPU and memory included and real
  spot prices.
