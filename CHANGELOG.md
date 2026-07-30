# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Split the monolithic `compose.yml` into role-based include files
  (`core`, `cloud`, `media`, `home`, `monitoring`).
- Moved routing to Traefik's file provider (`traefik/dynamic.yml`), rendered from a
  template by a `config-render` service. Dropped Docker-label routing.
- Switched TLS to a single `*.${DOMAIN}` wildcard issued by acme.sh and served as
  Traefik's default certificate. Removed Traefik's own ACME resolver.
- Made LAN-only the default; Cloudflare Tunnel and WireGuard are now opt-in profiles.

- Moved the Cloudflare Tunnel token out of `command:` into the environment; a
  command line is readable by any user on the host via `docker inspect`.
- Rendered `traefik/dynamic.yml` with `sed` instead of `envsubst`, dropping the
  `apk add gettext` that made every `up` depend on a package mirror.
- Deduplicated the *arr services onto YAML anchors, and gave every container log
  rotation, so a single service can no longer fill the disk.
- Gated Paperless and the Redis caches behind healthchecks rather than start
  order, matching what Nextcloud already did.

### Added

- A news digest pipeline: Miniflux (+ Postgres) under the `miniflux` profile,
  RSS-Bridge under `rssbridge`, and `news-digest` + an optional `fb-scraper`
  under `digest`. One LLM-summarised email a day, plus a LAN-only status page
  whose "Run now" button renders a digest without emailing or marking read.
- Traefik routes for Bazarr, Tdarr and qBittorrent, which previously ran on the
  proxy network with no way to reach them.

- Beszel (hub + agent) and a Trivy vuln-scanner under the `monitoring` profile.
- Gitea, Homarr, and Uptime Kuma under the `cloud` profiles.
- `no-new-privileges` on every service, CPU/memory caps on the heavy containers, and
  basic auth on admin UIs without their own login.
- Community files: CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue/PR templates, CI.
- Nextcloud cron container and a MariaDB healthcheck so Nextcloud waits on an
  initialised database, plus documented DOTNET workstation-GC toggles for the
  .NET media apps.
- Local `justfile` and CI checks (yamllint, shellcheck, markdownlint, `.env.example`
  coverage), Dependabot for GitHub Actions, and a scheduled Trivy image scan.

### Removed

- Podman Quadlet support (`quadlet/`, the unit generator and installer, and the
  CI drift check). The stack is Docker Compose only.
- Standalone `arr-stack.yml` (folded into `media.yml`).
