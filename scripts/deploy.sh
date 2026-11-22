#!/usr/bin/env bash
# Deploy a git ref (branch, tag or commit) of AVIRO to the local Podman stack,
# then emit a deployment event.
#
#   scripts/deploy.sh scenario/S01-bad
#   scripts/deploy.sh main
#
# The image is built from `git archive <ref>`, so your working tree is never touched.
# Deployment event: POSTed to $SENTINEL_DEPLOY_WEBHOOK_URL with header
# X-Sentinel-Key: $SENTINEL_API_KEY when both are set, otherwise printed to stdout.
#
# If AVIRO is connected to SENTINEL (`make up-sentinel` fills .sentinel.env), the
# rollout keeps that connection: the same overlay and telemetry settings.
set -euo pipefail

ref="${1:?usage: scripts/deploy.sh <git-ref>}"
cd "$(dirname "$0")/.."

compose=(podman compose)
if [[ -s .sentinel.env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.sentinel.env
  set +a
  compose+=(-f compose.yaml -f compose.sentinel.yaml)
  export OTEL_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://ingest.sentinel.internal:4319
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20${AVIRO_SENTINEL_TOKEN}"
fi

sha="$(git rev-parse --verify "${ref}^{commit}")"
short="${sha:0:12}"
version="$(git describe --tags --exact-match "$sha" 2>/dev/null || echo "$short")"
image="localhost/aviro:${short}"

echo "==> building ${image} from ${ref} (${version})"
git archive --format=tar "$sha" | podman build --quiet --build-arg "AVIRO_VERSION=${version}" -t "$image" -f Containerfile - >/dev/null

echo "==> rolling out"
AVIRO_IMAGE="$image" AVIRO_VERSION="$version" \
  "${compose[@]}" up -d --force-recreate --no-deps inventory payments orders worker gateway >/dev/null

deployed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
for service in gateway inventory payments orders worker; do
  event=$(printf '{"service":"%s","version":"%s","image_ref":"%s","commit_sha":"%s","deployed_at":"%s","deployed_by":"%s","source":"script"}' \
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
