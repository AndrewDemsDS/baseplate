# Contributing

Thanks for considering a contribution. This repo is a Docker Compose template, so
most changes touch YAML, the Traefik config, or the docs.

## Ground rules

- Keep it single-host and Compose v2. No Kubernetes, no Swarm.
- Keep personal data out of the repo. Use `example.com`, `you@example.com`, and
  generic paths. No real domains, IPs, tokens, or password hashes in committed files.
- New services are opt-in. Put them behind a `profiles:` entry so a default `up`
  stays small.
- Pin stateful apps (databases, Nextcloud, Gitea) and add them to Watchtower's
  `--disable-containers` list.

## Before you open a PR

Run the full local check suite:

```sh
just lint   # yamllint + shellcheck + markdownlint
just test   # compose config (merged + every profile) + env coverage + Traefik render
```

Or run the same commands directly:

```sh
# render the Traefik config and validate the output is valid YAML
DOMAIN=example.com envsubst '$DOMAIN' < traefik/dynamic.yml.template \
  | python3 -c 'import sys,yaml; yaml.safe_load(sys.stdin)'

# compose files parse and merge
docker compose config >/dev/null
for p in nextcloud vaultwarden gitea homarr kuma paperless media vpn assistant mqtt monitoring tunnel wireguard ddns; do
  docker compose --profile "$p" config >/dev/null
done

# lint
yamllint -c .yamllint compose.yml core.yml cloud.yml media.yml home.yml monitoring.yml traefik/*.yml
shellcheck -S warning scripts/*.sh
npx -y markdownlint-cli2 '**/*.md' --config .markdownlint.jsonc

# ensure .env.example documents every environment variable used in the stack
sh scripts/check-env-coverage.sh
```

CI runs all of the above plus a secret scan on every PR.

## Commit and PR conventions

- One change per PR. Describe what it does and why.
- Fill in the AI-assistance box in the PR template.
- Squash noise before review.

## AI assistance

If AI tooling helped with a change, say so in the PR. Use a generic
`Assisted-by: AI` trailer on the commit rather than naming a vendor.
