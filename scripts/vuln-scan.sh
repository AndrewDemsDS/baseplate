#!/bin/sh
# Scan every image tag used in the stack for HIGH/CRITICAL CVEs and email a
# summary. Reads image tags from the mounted compose files (/stack), so it needs
# no Docker socket. Runs on a loop; SCAN_INTERVAL seconds between scans.
#
# Env: RESEND_API_KEY, ALERT_EMAIL, ALERT_FROM, SCAN_INTERVAL (default weekly).
set -eu

INTERVAL="${SCAN_INTERVAL:-604800}"

scan_once() {
  images=$(grep -rhoE '^\s*image:\s*\S+' /stack 2>/dev/null \
            | sed -E 's/^\s*image:\s*//; s/["'\'']//g' | sort -u)
  [ -z "$images" ] && { echo "no images found under /stack"; return; }

  report=""
  for img in $images; do
    echo "scanning $img"
    out=$(trivy image --quiet --scanners vuln --severity HIGH,CRITICAL \
            --no-progress "$img" 2>/dev/null || true)
    if echo "$out" | grep -q "Total: [1-9]"; then
      report="${report}
=== ${img} ===
${out}
"
    fi
  done

  if [ -z "$report" ]; then
    echo "no HIGH/CRITICAL findings"
    return
  fi

  echo "$report"
  if [ -n "${RESEND_API_KEY:-}" ]; then
    body=$(printf '%s' "$report" | head -c 60000 | sed 's/"/\\"/g' | awk '{printf "%s\\n", $0}')
    curl -s -X POST https://api.resend.com/emails \
      -H "Authorization: Bearer ${RESEND_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "{\"from\":\"${ALERT_FROM}\",\"to\":\"${ALERT_EMAIL}\",\"subject\":\"baseplate: image CVEs found\",\"text\":\"${body}\"}" \
      >/dev/null && echo "alert emailed to ${ALERT_EMAIL}"
  fi
}

while true; do
  scan_once || echo "scan failed"
  sleep "$INTERVAL"
done
