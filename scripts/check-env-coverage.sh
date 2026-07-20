#!/bin/sh
# Ensure .env.example covers every environment variable referenced in the
# compose files. Variables that already have a default (${VAR:-...}) are allowed
# to be missing, but everything else must be documented in .env.example.
set -eu

# The quadlet units are included so Podman-only vars (BASEPLATE_DIR,
# PODMAN_NET_MTU) count as used rather than showing up as unused extras.
compose_files="compose.yml core.yml cloud.yml media.yml home.yml monitoring.yml traefik/dynamic.yml.template"
quadlet_files=$(find quadlet -type f 2>/dev/null || true)

# Extract ${VAR}, ${VAR:-default}, ${VAR:?message}, ${VAR:=...} from the files.
compose_vars=$(grep -rhoE '\$\{[A-Za-z_][A-Za-z0-9_]*([:=?-][^}]+)?\}' $compose_files $quadlet_files 2>/dev/null \
  | sed -E 's/^\$\{([A-Za-z_][A-Za-z0-9_]*).*/\1/' \
  | sort -u)

env_vars=$(grep -hoE '^[A-Za-z_][A-Za-z0-9_]*=' .env.example \
  | sed 's/=$//' \
  | sort -u)

missing=0
optional=0
extra=0

for v in $compose_vars; do
  if echo "$env_vars" | grep -qx "$v"; then
    continue
  fi
  if grep -rqE '\$\{'"$v"':-[^}]*\}' $compose_files 2>/dev/null; then
    optional=$((optional + 1))
    echo "OPTIONAL (has default): $v"
  else
    missing=$((missing + 1))
    echo "MISSING from .env.example: $v"
  fi
done

for v in $env_vars; do
  if ! echo "$compose_vars" | grep -qx "$v"; then
    extra=$((extra + 1))
    echo "EXTRA in .env.example (unused): $v"
  fi
done

echo "compose/template vars: $(echo "$compose_vars" | wc -l); .env.example vars: $(echo "$env_vars" | wc -l); missing: $missing; defaulted-optional: $optional; extra: $extra"

[ "$missing" -eq 0 ]
