# Local task runner for baseplate.
# Install `just` (https://just.systems), then run `just` to list recipes.

set shell := ["bash", "-cu"]

_default:
    @just --list

# Run all lint checks.
lint: lint-yaml lint-shell lint-md

# Lint YAML with yamllint.
lint-yaml:
    yamllint -c .yamllint compose.yml core.yml cloud.yml media.yml home.yml monitoring.yml traefik/*.yml .github/workflows/*.yml

# Lint shell scripts with shellcheck.
lint-shell:
    shellcheck -S warning scripts/*.sh

# Lint Markdown with markdownlint-cli2.
lint-md:
    npx -y markdownlint-cli2 '**/*.md' --config .markdownlint.jsonc

# Run all tests / validations.
test: test-render test-compose test-env

# Render traefik/dynamic.yml.template and validate the result is valid YAML.
test-render:
    DOMAIN=example.com envsubst '$DOMAIN' < traefik/dynamic.yml.template \
      | python3 -c 'import sys,yaml; yaml.safe_load(sys.stdin); print("dynamic.yml OK")'

# Validate the full merged compose config and each profile.
test-compose:
    #!/usr/bin/env bash
    set -euo pipefail
    export DOMAIN=example.com CF_DNS_API_TOKEN=dummy
    docker compose config >/dev/null
    for p in nextcloud vaultwarden gitea homarr kuma paperless media vpn assistant mqtt monitoring tunnel wireguard ddns; do
      echo "profile: $p"
      docker compose --profile "$p" config >/dev/null
    done

# Ensure .env.example covers the variables used in the compose files.
test-env:
    sh scripts/check-env-coverage.sh

# Scan every container image used in the stack for HIGH/CRITICAL CVEs.
scan:
    sh scripts/ci-image-scan.sh
