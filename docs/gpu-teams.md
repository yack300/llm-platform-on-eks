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
| product | `llm-platform` | vLLM (the RAG model) | the on-demand one, time-sliced in 2 | never interrupted: not by AWS (on-demand), not by research (it reclaims) |
| research | `training` | fine-tuning workflows | the spot one, whole | waits if it's taken or there's no spot capacity |
| research, small jobs | `training` | anything that fits in half a T4 | a share of product's GPU, borrowed | evicted when product needs it |

The account's GPU quota allows one g4dn.xlarge spot and one on-demand.

## How it works

- **Kueue** (`k8s/teams/`, Argo CD app `kueue`, wave 0) admits GPU pods
  against the quotas. The ClusterQueues (`product`, `research`,
  `research-shared`) are in one cohort (`gpus`), so unused quota can be
  lent, each with a LocalQueue in its team's namespace. Only
  `research-shared` borrows: it has no quota of its own.
- **Flavors** map the quota to capacity types: `gpu-on-demand` and
  `gpu-spot`. Each is a Karpenter NodePool (`gpu-shared`, `gpu`), limited to
  1 GPU. Kueue adds the flavor's `karpenter.sh/capacity-type` to the
  admitted pod's node selector, and Karpenter launches that kind of node.
- **product**'s queue reclaims its own quota from a borrower
  (`reclaimWithinCohort: Any`); research's queues never preempt anyone.
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

With the first split: both GPUs in 2 shares, and one research queue that
borrowed. [Preparing EKS](#preparing-eks-what-the-first-split-got-wrong)
changed it.

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
  *failed*: the condition was inverted. Already fixed upstream
  ([opencost#4173](https://github.com/opencost/opencost/pull/4173), merged
  2026-11-13) but in no release yet: 1.121.4 is three days older. The fix
  still needs those two labels on the nodes, and Bottlerocket runs no GPU
  Feature Discovery, so the GPU NodePool will have to set them.
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

## Preparing EKS: what the first split got wrong

Checked on paper before paying for a cluster:

- **Two vLLM replicas don't fit a node.** Each requests 8 GiB of memory and
  a g4dn.xlarge has 16. KEDA's maximum is now 1 (it was 3).
- **Training can't share a T4**, and it can't ask for both shares either: a
  pod requesting `nvidia.com/gpu: 2` would never get a node, because
  Karpenter sizes a g4dn.xlarge as 1 GPU. Borrowing one share, it would land
  next to vLLM and one of them would run out of memory. So the spot GPU
  isn't sliced and training only goes there; small jobs borrow, from a queue
  of their own (`borrowingLimit: 1`).
- **Karpenter would launch a node per pending share** for the same reason.
  Each GPU NodePool is limited to 1 GPU, the account's quota anyway.
- **vLLM took 85% of the GPU's memory**; now 45%. Its KV cache shrinks with
  it, so part of what phase C measures is that and not the sharing: the
  baseline has to be taken again at 45%.
- **OpenCost needs GPU Feature Discovery's labels**, which Bottlerocket
  doesn't set: the `gpu-shared` NodePool does.
- The price of "never interrupted": vLLM now always runs on demand
  ($0.526/h, against ~$0.27 when spot was available), and training has no
  on-demand fallback. Most runs so far found no spot capacity.

Time-slicing is `settings.kubelet-device-plugins.nvidia` in the `gpu-shared`
EC2NodeClass's `userData`; Karpenter 1.15.0 validates only
`settings.kubernetes` and passes the rest of the TOML through (its
`bottlerocketsettings.go`). Neither has run on EKS yet.

The new split on kind (2026-11-24, the same two scripts):

| Step | What happened | Time |
|---|---|---|
| research submits two trainings and a small job | one training on spot, the small job on a borrowed share, the second training waits | 11 s |
| product asks for both on-demand shares | the small job is evicted and retried as a pod that waits; the running training isn't touched | 2 s |
| product scales to 0 | the small job is admitted again | 5 s |

And 18 minutes of it priced: the nodes cost $0.2388 and the report assigns
$0.2388 ($0.1096 product, $0.1292 research); OpenCost bills $0.2781. On the
spot node, not sliced, OpenCost and the report agree: $0.0765 for the
training.

## Phase C: the EKS session, planned

About 2 hours of cluster and 1.5 of on-demand GPU: $2-4.

1. Cluster and Argo CD as always; `kueue`, `teams` and `opencost` Healthy.
2. Wake vLLM. Check its node: `nvidia.com/gpu: 2` allocatable, the two
   labels, one node only. How long Kueue's admission adds to the cold start.
3. `scripts/gpu-interference.sh`: vLLM alone at 45%, then with a job in the
   other share.
4. `LOAD_GIB=12 ONLY_LOAD=1 scripts/gpu-interference.sh`: a job that doesn't
   fit. Which of the two fails?
5. Eviction: a pod in product's queue asking for the second share while the
   job runs.
6. A fine-tuning workflow on spot, if there's capacity (it'd also show what
   a spot reclaim does), with vLLM serving.
7. `scripts/gpu-chargeback.py --price on-demand=0.526 --price spot=<that
   day's>` for the session; Cost Explorer two days later.
8. `scripts/teardown-eks.sh`, with the queues emptied first.

Not answered by the plan: how Kueue's 2 shares behave if the node is slow to
come (a Workload is admitted against quota, not capacity), and who pays for
the node's CPU and memory besides the GPU's holder.
