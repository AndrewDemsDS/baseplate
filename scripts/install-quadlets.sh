#!/usr/bin/env bash
# Install baseplate Quadlet units into /etc/containers/systemd.
#
# Compose has profiles; Quadlet does not. The equivalent here is choosing which
# profile directories to install: units you do not copy simply do not exist.
#
#   ./scripts/install-quadlets.sh nextcloud media monitoring
#
# `base` is always installed (networks, traefik, acme, socket-proxy, watchtower).
# Re-running is safe: it overwrites the units it manages and leaves others alone.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${UNIT_DIR:-/etc/containers/systemd}"
ENV_FILE="${ENV_FILE:-${REPO_DIR}/.env}"
DRY_RUN="${DRY_RUN:-0}"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found (copy .env.example and fill it in)" >&2
  exit 1
fi

# BASEPLATE_DIR is referenced by units that bind-mount repo files; Quadlet needs
# absolute paths. PODMAN_NET_MTU guards tunnelled uplinks where 1500 silently
# breaks image pulls -- harmless to leave at 1500 on a clean path.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
BASEPLATE_DIR="${BASEPLATE_DIR:-$REPO_DIR}"
PODMAN_NET_MTU="${PODMAN_NET_MTU:-1500}"
set +a

# Compose runs this as a one-shot `config-render` container because it has no
# pre-start hook. Here it is just a command. Note the single quotes: envsubst
# must replace only $DOMAIN, or it would eat Traefik's own $-syntax.
if [ "$DRY_RUN" != "1" ]; then
  envsubst '$DOMAIN' \
    < "${REPO_DIR}/traefik/dynamic.yml.template" \
    > "${REPO_DIR}/traefik/dynamic.yml"
  echo "rendered traefik/dynamic.yml"
fi

profiles=("base" "$@")
installed=0

for profile in "${profiles[@]}"; do
  src="${REPO_DIR}/quadlet/${profile}"
  if [ ! -d "$src" ]; then
    echo "error: unknown profile '${profile}' (no ${src})" >&2
    exit 1
  fi
  for unit in "$src"/*; do
    [ -e "$unit" ] || continue
    name="$(basename "$unit")"
    if [ "$DRY_RUN" = "1" ]; then
      echo "would install ${name} (${profile})"
    else
      envsubst < "$unit" > "${UNIT_DIR}/${name}"
      # Substitution bakes secrets from .env into the unit, so keep them
      # root-only rather than the default 0644.
      chmod 600 "${UNIT_DIR}/${name}"
      echo "installed ${name} (${profile})"
    fi
    installed=$((installed + 1))
  done
done

if [ "$DRY_RUN" = "1" ]; then
  echo "dry run: ${installed} unit(s) would be installed to ${UNIT_DIR}"
  exit 0
fi

# Skipped when installing somewhere other than the live unit dir (CI, testing),
# where there is no systemd to talk to.
if [ "$UNIT_DIR" = "/etc/containers/systemd" ] && command -v systemctl >/dev/null; then
  systemctl daemon-reload
fi
echo
echo "${installed} unit(s) installed. Start them with:"
echo "  systemctl start traefik.service   # and the services you enabled"
echo "Units are WantedBy=default.target, so they come back after a reboot."
