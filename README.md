# baseplate

A single-host self-hosting stack you can clone and run. Docker Compose, split by
role, with opt-in profiles. Traefik terminates TLS with one wildcard certificate;
everything is LAN-only by default, and remote access is a profile you turn on.

Tested on a Raspberry Pi 5 (Debian 13) and an x86 mini-PC/NAS. Any Docker host works.

## What you get

| Service | Profile | Purpose |
| --------- | --------- | --------- |
| Traefik | (default) | Reverse proxy, file-provider routing, wildcard TLS |
| acme.sh | (default) | Issues/renews `*.${DOMAIN}` over Cloudflare DNS-01 |
| socket-proxy + Watchtower | (default) | Nightly updates without the raw Docker socket |
| Nextcloud (+ MariaDB, Redis) | `nextcloud` | Files, calendar, contacts |
| Vaultwarden | `vaultwarden` | Bitwarden-compatible password manager |
| Gitea | `gitea` | Self-hosted git |
| Homarr | `homarr` | Start-page dashboard |
| Uptime Kuma | `kuma` | Status/uptime monitoring |
| Paperless-ngx (+ Postgres, Redis) | `paperless` | Document archive |
| Miniflux (+ Postgres) | `miniflux` | RSS/Atom feed reader with a real API |
| RSS-Bridge | `rssbridge` | HTML-to-RSS shim for sites without a feed |
| news-digest (+ fb-scraper) | `digest` | One LLM-summarised email a day from your feeds |
| Jellyfin + Sonarr/Radarr/Prowlarr/Bazarr/Tdarr | `media` | Media server + automation |
| qBittorrent + Gluetun | `vpn` | Torrent client behind a VPN |
| Home Assistant | `assistant` | Smart-home hub (host network) |
| Mosquitto | `mqtt` | MQTT broker (auth required) |
| Beszel + agent | `monitoring` | Host/container metrics |
| Trivy vuln-scanner | `monitoring` | Weekly image CVE scan + email |
| Cloudflare Tunnel | `tunnel` | Public access, no port-forwarding |
| WireGuard | `wireguard` | VPN back into the LAN |
| Cloudflare DDNS | `ddns` | Direct-IP A-record updates |

## How it fits together

- **Routing** comes from a file, `traefik/dynamic.yml`, not Docker labels. You edit
  `traefik/dynamic.yml.template`; a one-shot `config-render` service substitutes
  `${DOMAIN}` and writes the live file before Traefik starts.
- **TLS** is a single `*.${DOMAIN}` wildcard. acme.sh issues it over Cloudflare
  DNS-01 and drops it in a shared volume; Traefik serves it as the default
  certificate. No per-host ACME, no Traefik-managed `acme.json`.
- **Ingress** defaults to LAN-only: every route carries an `ipAllowList` for
  private ranges. Turn on the `tunnel` or `wireguard` profile to reach it from
  outside.
- **Docker access** for Watchtower goes through a read-only socket-proxy on an
  internal network. Traefik mounts no socket at all.
- **Hardening**: `no-new-privileges` on every service, internal-only networks for
  databases, CPU/memory caps on the heavy containers, security headers on every
  route, and basic auth in front of admin UIs that lack their own login.
- **Startup order** is enforced with healthchecks, not sleeps: anything with a
  database or cache waits on `condition: service_healthy`, so an app never comes
  up against a half-initialised backend.
- **Logs are capped** at 10 MB × 3 files per container. Docker's default
  `json-file` driver has no limit, which is how a chatty container fills a disk.

## Requirements

- A Docker host with Compose v2.
- A domain on Cloudflare and an API token (Zone Read + DNS Edit, scoped to the zone).
- Local DNS (or `/etc/hosts`) pointing `*.${DOMAIN}` at the host for LAN-only use.

## Quick start

```sh
cp .env.example .env
# set DOMAIN and CF_DNS_API_TOKEN, plus any app secrets you need

# bring up the proxy + a couple of apps
docker compose --profile nextcloud --profile monitoring up -d
```

`config-render` writes `traefik/dynamic.yml` from the template on every `up`.

## TLS: issue the wildcard once

acme.sh runs as a daemon and renews automatically, but the first issue is manual:

```sh
docker compose exec acme acme.sh --issue --dns dns_cf -d "${DOMAIN}" -d "*.${DOMAIN}" \
  --server letsencrypt
docker compose exec acme acme.sh --install-cert -d "${DOMAIN}" \
  --key-file /certs/wildcard.key --fullchain-file /certs/wildcard.crt
docker compose restart traefik
```

Until the cert exists, Traefik serves a self-signed default. Browsers will warn.

## Profiles

Compose only starts a service when its profile is selected:

```sh
docker compose --profile nextcloud --profile media --profile monitoring up -d
```

Routes for services you do not run stay in `dynamic.yml` and return 502 until the
service is up. Comment out the ones you skip, or leave them.

## App notes

**Vaultwarden.** Hash the admin token; do not store it in plaintext:

```sh
docker run --rm vaultwarden/server:latest /vaultwarden hash
```

Put the resulting `$argon2id$...` string in `VAULTWARDEN_ADMIN_TOKEN`.

**MQTT.** The broker ships with `allow_anonymous false`, so create the passwd file
and a user before enabling the profile:

```sh
touch mosquitto/passwd
docker compose --profile mqtt up -d mosquitto
docker compose exec mosquitto mosquitto_passwd -b /mosquitto/config/passwd user pass
docker compose restart mosquitto
```

**Gitea.** Git-over-SSH is published on host port 2222. Clone with
`git clone ssh://git@git.${DOMAIN}:2222/owner/repo.git`.

**Jellyfin transcoding.** Uncomment the `devices` block in `media.yml`. On x86 use
`/dev/dri` (VAAPI/QuickSync); on a Pi 5 use the `/dev/video1{0,1,2}` V4L2 devices.

**Beszel.** Start the hub, create the admin user, copy its public key into
`BESZEL_KEY`, then recreate the agent so it trusts the hub.

**News digest.** Miniflux collects the feeds; `news-digest` summarises the last 24
hours into one email a day. The `digest` profile pulls Miniflux up with it.

```sh
docker compose --profile digest up -d
# mint an API key once Miniflux is healthy, then put it in MINIFLUX_API_KEY
docker compose exec miniflux sh -c 'wget -qO- --header="Content-Type: application/json" \
  --post-data="{\"description\":\"digest\"}" \
  --http-user=$ADMIN_USERNAME --http-password=$ADMIN_PASSWORD \
  http://127.0.0.1:8080/v1/api-keys'
```

Add your feeds in the Miniflux UI at `reader.${DOMAIN}`. Set `DIGEST_REGION` to
whatever the "national news" section should be called; everything is translated
into `DIGEST_LANGUAGE` whatever the sources are in.

`digest.${DOMAIN}` shows a status page with a **Run now** button. That button
renders a fresh digest in the browser and deliberately does not email and does not
mark entries read, so the scheduled run still goes out complete.

Two things worth knowing before you rely on it:

- **Verify a sender domain in Resend first.** Any `DIGEST_FROM` on an unverified
  domain returns 403 on every send.
- **Free LLM tiers cap tokens per minute far below the context window**, so the
  digest is deliberately map-reduce: `GROQ_CHUNK_SIZE` items per call with
  `GROQ_CHUNK_DELAY` seconds between. A whole day in one call is an instant 429.
  Roughly 12k tokens per run at 75 items. `GROQ_BASE_URL` accepts any
  OpenAI-compatible endpoint if you would rather run inference locally.

Every failure degrades instead of cancelling the run: if the summariser is down you
still get headlines with working links, and a red banner saying why.

**fb-scraper (optional).** Reads PUBLIC Facebook groups logged out and feeds them
into the digest. It never logs in and holds no credentials, so the worst case is a
run that returns nothing. Set `FB_GROUPS` to comma-separated group slugs; leave it
empty to disable. Note the ceiling: logged out, Facebook serves 3-5 posts per fetch
and scrolling adds nothing, so it polls several times a day and dedupes. Expect it
to break whenever Meta reshuffles its payloads. RSS-Bridge's FacebookBridge is not
an alternative: upstream's own docs say groups "do not work at all".

## Remote access

Default is LAN-only. Pick one:

- **Cloudflare Tunnel** (`--profile tunnel`): create a remotely-managed tunnel, set
  `CLOUDFLARED_TUNNEL_TOKEN`, and add Public Hostnames pointing at `http://traefik:80`.
  No router ports to open.
- **WireGuard** (`--profile wireguard`): open UDP 51820, set `WG_SERVER_HOSTNAME`,
  and connect with the generated peer configs. Nothing else faces the internet.

## Updating

Watchtower updates running containers nightly and skips the stateful apps
(`nextcloud`, `vaultwarden`, `gitea`, `paperless` and their databases) so a major
version never lands unattended. Bump those tags yourself when you are ready.

## AI assistance

Parts of this template and its docs were written with AI assistance. I run the
stack myself and tested the configuration before publishing it.

## Development

The repo ships with a GitHub Actions CI pipeline and a local `justfile` so you
can run the same checks before pushing:

```sh
just lint   # yamllint, shellcheck, markdownlint
just test   # compose config (all profiles), env coverage, Traefik render
just scan   # local Trivy scan of every image used in the stack
```

CI runs on every PR and push to `main`:

- compose config validation (merged + every profile)
- rendered `traefik/dynamic.yml` YAML check
- `.env.example` coverage check
- yamllint, shellcheck, markdownlint
- gitleaks secret scan

A scheduled weekly Trivy image scan uploads results to the GitHub Security tab.
Dependabot covers GitHub Actions; container images are kept current by Watchtower
at runtime, since Dependabot cannot parse image tags out of Compose files.

## License

MIT. See [LICENSE](LICENSE).
