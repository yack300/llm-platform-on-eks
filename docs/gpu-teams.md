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
`bottlerocketsettings.go`), as the EKS session confirmed.

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

## Phase C: tenth EKS run (2026-11-25)

45 minutes from Argo CD's install to the start of the teardown, with one
on-demand g4dn.xlarge. Raw results: `docs/results/2026-11-25-gpu-teams/`.

**Time-slicing works.** The node advertises `nvidia.com/gpu: 2`, with the
two labels, and Karpenter launched only one. vLLM starts at 45% of the
memory with room for 9,344 tokens of KV cache (2.3 requests of the maximum
length at once). Kueue's admission adds nothing to the cold start: 2 s from
the pod to Karpenter's NodeClaim. The whole cold start took 676 s, without
the image cache: ~4 min 40 s of images and adapters and 5 min 31 s of vLLM's
own startup.

**Sharing the GPU makes vLLM 2.5 times slower**, with a job multiplying
matrices without pause in the other share (`scripts/gpu-interference.sh`):

| | vLLM alone | Sharing | |
|---|---|---|---|
| Output tokens/s, 1 request at a time | 46.9 | 18.6 | 2.5x slower |
| Output tokens/s, 4 at a time | 170.9 | 68.4 | 2.5x slower |
| Latency p50, 1 at a time | 2.73 s | 6.87 s | 2.5x |
| Time to first token p50, 1 at a time | 0.32 s | 0.39 s | +22% |
| The other job, matmul/s | 30 | 17.5 | -42% |

One run, 6 and 24 requests of 128 tokens. The job is a worst case: it never
waits. Half the time each would be 2x; the rest wasn't profiled.

**Memory is first come, first served**, and that can take serving down:

- A 12 GiB job next to a running vLLM fails at once (`CUDA out of memory`,
  7.78 GiB free). vLLM kept answering.
- A 10 GiB job that is there *first*, when vLLM restarts: vLLM can't start
  (`Free memory on device (4.14/14.56 GiB) ... less than desired (0.45, 6.55
  GiB)`), fails and is restarted until the job is gone. Serving was down for
  7 minutes, 2 of them by the job and 5 of vLLM's startup. Kueue admitted
  both, correctly: it counts shares. Product's right to take its share back
  didn't help either, because it already had one.

**Eviction:** product claiming the second share ran after 35 s. Kueue
preempted the job at once; most of the rest is probably the evicted pod's
30 s termination grace period (2-11 s on kind). A job that borrows should set a short one.

**No spot capacity, as on most runs** (placement score 1 in every zone): the
training workflow waited 9 minutes for a node that never came
(`UnfulfillableCapacity`). Kueue had admitted it, since there was quota, so
it also held research's whole quota while having no GPU.

**Chargeback**, 25.7 minutes of the GPU node at $0.526/h:

| | Used | Idle | Owed | OpenCost |
|---|---|---|---|---|
| product | $0.0833 | $0.1030 | $0.1863 | $0.1386 |
| research | $0.0394 | | $0.0394 | $0.0657 |
| GPU node | | | **$0.2257** | $0.2042 |

OpenCost prices the node right ($0.526/h, from AWS) and counts 1 GPU thanks
to the labels. It only counts *running* containers: vLLM held its share from
its pod's creation but spent 5 minutes pulling images, 20% of the session,
which is nobody's usage (here it falls to product as idle, who should pay it;
a borrower's would fall to product too). A pod that lived 30 s doesn't show
up at all. To be compared with Cost Explorer.

**Found on the way:**

- Argo CD needs Kueue's OCI registry declared in `argocd-values.yaml`, like
  Karpenter's.
- Kueue's aggregated API (`visibility`) listens on 8082, which the EKS
  module's security group doesn't open from the control plane. Its
  APIService failed, which kept the `kueue` app Progressing and wave 0
  waiting; `terraform/main.tf` opens it now.
- Emptying the queues before the teardown's cascade worked: nothing got
  stuck.

**What this says about sharing a T4 this way:** it's fine for a job that can
be slow next to a service that can be slow. For a service with a latency
target it isn't, whatever the quotas say, and a memory limit has to come
from somewhere else. Not tried: MPS, which Bottlerocket also offers, and
which can limit each client's memory.
