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

Run these locally:

```sh
# compose files parse and merge
docker compose config >/dev/null
docker compose --profile nextcloud --profile media --profile monitoring config >/dev/null

# the rendered Traefik config is valid YAML
DOMAIN=example.com envsubst '$DOMAIN' < traefik/dynamic.yml.template \
  | python3 -c 'import sys,yaml; yaml.safe_load(sys.stdin)'

# lint
yamllint -d '{extends: default, rules: {line-length: disable}}' .
shellcheck scripts/*.sh
```

CI runs the same checks plus a secret scan on every PR.

## Commit and PR conventions

- One change per PR. Describe what it does and why.
- Fill in the AI-assistance box in the PR template.
- Squash noise before review.

## AI assistance

If AI tooling helped with a change, say so in the PR. Use a generic
`Assisted-by: AI` trailer on the commit rather than naming a vendor.
