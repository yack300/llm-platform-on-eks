#!/usr/bin/env bash
# Pushes a locally built image to GHCR and signs it keylessly, for ci.yml's
# image jobs on main. Tags are immutable: an image:tag already published is
# left alone (bump the app's VERSION to release changes).
#
#   .github/scripts/push-and-sign.sh ghcr.io/yack300/<image>:<tag>
#
# Needs GITHUB_TOKEN (packages: write), the job's OIDC token (id-token:
# write) and COSIGN_IMAGE. Keyless: cosign trades the workflow's OIDC token
# for a short-lived certificate from Fulcio, signs the digest and records it
# in Rekor. The identity Kyverno checks is ci.yml on main.
set -euo pipefail
ref="$1"

echo "$GITHUB_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin
if docker manifest inspect "$ref" >/dev/null 2>&1; then
  echo "::notice::$ref is already published; bump its VERSION to release changes."
  exit 0
fi
docker push "$ref"
digest=$(docker inspect --format '{{index .RepoDigests 0}}' "$ref")
# Runner's uid so it can read the owner-only docker login file.
docker run --rm --user "$(id -u)" -e HOME=/tmp \
  -v "$HOME/.docker":/docker -e DOCKER_CONFIG=/docker \
  -e ACTIONS_ID_TOKEN_REQUEST_URL -e ACTIONS_ID_TOKEN_REQUEST_TOKEN \
  "$COSIGN_IMAGE" sign --yes "$digest"
echo "::notice::Published and signed $digest"
