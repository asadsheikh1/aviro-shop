#!/usr/bin/env bash
# Deploy a git ref (branch, tag or commit) of AVIRO to the local Podman stack,
# then emit a deployment event.
#
#   scripts/deploy.sh scenario/S01-bad
#   scripts/deploy.sh main
#
# The image is built from `git archive <ref>`, so your working tree is never touched.
# Deployment event: POSTed to $SENTINEL_DEPLOY_WEBHOOK_URL with header
# X-Sentinel-Key: $SENTINEL_API_KEY when both are set (wired up in SENTINEL Step 10),
# otherwise printed to stdout.
set -euo pipefail

ref="${1:?usage: scripts/deploy.sh <git-ref>}"
cd "$(dirname "$0")/.."

sha="$(git rev-parse --verify "${ref}^{commit}")"
short="${sha:0:12}"
version="$(git describe --tags --exact-match "$sha" 2>/dev/null || echo "$short")"
image="localhost/aviro:${short}"

echo "==> building ${image} from ${ref} (${version})"
git archive --format=tar "$sha" | podman build --quiet --build-arg "AVIRO_VERSION=${version}" -t "$image" -f Containerfile - >/dev/null

echo "==> rolling out"
AVIRO_IMAGE="$image" AVIRO_VERSION="$version" \
  podman compose up -d --force-recreate --no-deps inventory payments orders worker gateway >/dev/null

deployed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
for service in gateway inventory payments orders worker; do
  event=$(printf '{"service":"%s","version":"%s","image_ref":"%s","commit_sha":"%s","deployed_at":"%s","deployed_by":"%s"}' \
    "$service" "$version" "$image" "$sha" "$deployed_at" "${USER:-unknown}")
  if [[ -n "${SENTINEL_DEPLOY_WEBHOOK_URL:-}" && -n "${SENTINEL_API_KEY:-}" ]]; then
    curl -fsS -X POST "$SENTINEL_DEPLOY_WEBHOOK_URL" \
      -H "Content-Type: application/json" -H "X-Sentinel-Key: ${SENTINEL_API_KEY}" \
      -H "Idempotency-Key: deploy-${short}-${service}" -d "$event" >/dev/null
    echo "    event sent: ${service}"
  else
    echo "    deployment event (SENTINEL webhook not configured): ${event}"
  fi
done
echo "==> deployed ${version}"
