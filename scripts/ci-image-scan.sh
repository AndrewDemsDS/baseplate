#!/bin/sh
# Scan every image tag used in the stack for HIGH/CRITICAL CVEs.
# This is the local/CI counterpart to the in-stack vuln-scanner container.
# Exits non-zero if any HIGH/CRITICAL findings are reported.
set -eu

images=$(grep -rhoE '^\s*image:\s*\S+' compose.yml core.yml cloud.yml media.yml home.yml monitoring.yml 2>/dev/null \
  | sed -E "s/^\s*image:\s*//; s/[\"']//g" \
  | sort -u)

[ -z "$images" ] && { echo "no images found"; exit 1; }

fail=0
for img in $images; do
  echo "=== scanning $img ==="
  if trivy image --quiet --scanners vuln --severity HIGH,CRITICAL --no-progress \
      --ignore-unfixed --ignore-policy .trivyignore.rego "$img" 2>/dev/null \
      | grep -q "Total: [1-9]"; then
    echo "FINDINGS in $img"
    fail=1
  else
    echo "OK"
  fi
done

[ "$fail" -eq 0 ] || exit 1
