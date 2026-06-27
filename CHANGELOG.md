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

### Added
- Beszel (hub + agent) and a Trivy vuln-scanner under the `monitoring` profile.
- Gitea, Homarr, and Uptime Kuma under the `cloud` profiles.
- `no-new-privileges` on every service, CPU/memory caps on the heavy containers, and
  basic auth on admin UIs without their own login.
- Community files: CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue/PR templates, CI.

### Removed
- Standalone `arr-stack.yml` (folded into `media.yml`).
