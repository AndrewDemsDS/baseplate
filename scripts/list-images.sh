#!/bin/sh
# Print a JSON array of every unique container image referenced in the compose
# stack. Used by the scheduled Trivy scan workflow.
set -eu

images=$(grep -rhoE '^\s*image:\s*\S+' compose.yml core.yml cloud.yml media.yml home.yml monitoring.yml 2>/dev/null \
  | sed -E "s/^\s*image:\s*//; s/[\"']//g" \
  | sort -u)

printf '%s\n' "$images" | jq -R . | jq -s -c .
