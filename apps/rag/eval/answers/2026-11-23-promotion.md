# Promotion gate, ninth EKS run (2026-11-23)

## Base (local-mistral) vs rag-behavior-v1

| check | base | candidate | rule | |
|---|---|---|---|---|
| correct | 35/56 | 36/56 | at most 1 fewer | pass |
| faithful | 35/56 | 43/56 | at most 1 fewer | pass |
| declines | 6/10 | 10/10 | at most 1 fewer | pass |
| language | 51/66 | 66/66 | must improve | pass |

**Promote** the candidate.

## Base (local-mistral) vs rag-behavior-v2

| check | base | candidate | rule | |
|---|---|---|---|---|
| correct | 35/56 | 33/56 | at most 1 fewer | **fail** |
| faithful | 35/56 | 44/56 | at most 1 fewer | pass |
| declines | 6/10 | 10/10 | at most 1 fewer | pass |
| language | 51/66 | 66/66 | must improve | pass |

**Do not promote** the candidate.
