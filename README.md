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

## Running under Podman (Quadlet)

Compose is the source of truth, but the stack also ships as [Podman
Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
units in `quadlet/`, so systemd supervises the containers directly with no
daemon and no compose runtime. This is what the author's own deployment runs.

The units are generated, not hand-written: `just gen-quadlets` regenerates
`quadlet/` from the compose files, and CI fails if the committed output has
drifted. Change compose, regenerate, commit.

```sh
cp .env.example .env      # set BASEPLATE_DIR to this checkout's absolute path
sudo ./scripts/install-quadlets.sh nextcloud media monitoring
sudo systemctl start traefik.service
```

`base` (networks, Traefik, acme.sh, socket-proxy, Watchtower) is always
installed; the arguments are profiles. Quadlet has no concept of profiles, so
the equivalent is simply not installing units you do not want. Units are
`WantedBy=default.target`, so they start on boot with no `restart:` policy
needed.

Things that differ from the compose path, and why:

- **`config-render` has no unit.** It exists only because compose cannot run
  a command before the stack starts; the installer renders
  `traefik/dynamic.yml` itself.
- **Bind mounts are absolute.** Quadlet rejects `./traefik`-style paths, so
  repo-relative mounts are rewritten against `BASEPLATE_DIR`.
- **MariaDB runs as `User=999`.** Its gosu privilege-drop hangs under rootful
  Podman with crun, so the container never reports healthy and everything
  waiting on it stalls. Docker and runc are unaffected.
- **Startup ordering is real.** `depends_on: condition: service_healthy`
  becomes `Notify=healthy` plus `TimeoutStartSec=300` on the dependency, so
  Nextcloud genuinely waits for MariaDB instead of racing it.
- **Installed units are `chmod 600`.** Substitution bakes `.env` values into
  them, so they must not be world-readable.
- **`PODMAN_NET_MTU`.** Leave at 1500 on a normal LAN. Behind PPPoE or a
  tunnelled uplink, an oversized MTU makes image pulls hang rather than fail,
  which is a genuinely confusing failure to debug. 1400 is the usual fix.

Rootless is untested here. The units assume rootful Podman, which is what
`PublishPort=80`/`443` and the socket mounts expect.

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

A scheduled weekly Trivy image scan uploads results to the GitHub Security tab,
and Dependabot keeps GitHub Actions and Docker base images current.

## License

MIT. See [LICENSE](LICENSE).
