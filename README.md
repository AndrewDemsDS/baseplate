# baseplate

An opinionated, single-host self-hosted stack. Drop a `.env`, pick the services you want with compose `profiles`, and you have a Traefik-fronted homelab reachable from anywhere via a Cloudflare Tunnel — no port forwarding, no public IP, no Dynamic DNS needed.

Tested on a Raspberry Pi 5 running Debian 13. Works on any Docker host (mini-PC, NAS, VPS).

## What you get

| Service | Why it's here |
| --- | --- |
| **Traefik v3** | Reverse proxy, automatic Let's Encrypt certs via Cloudflare DNS-01 |
| **Cloudflare Tunnel** | Outbound-only connection to Cloudflare's edge — your services on the internet without exposing any router ports |
| **Watchtower** | Nightly image updates with cleanup |
| **Nextcloud** *(opt-in)* | Files + calendar + contacts + Office collab |
| **Vaultwarden** *(opt-in)* | Bitwarden-compatible password manager |
| **Paperless-ngx** *(opt-in)* | Searchable document archive |
| **Mosquitto** *(opt-in)* | MQTT broker for home automation |
| **WireGuard** *(opt-in)* | VPN back into your LAN |
| **Cloudflare DDNS** *(opt-in)* | Classic A-record updates, if you want a direct-IP path alongside the tunnel |

The apps are all behind compose `profiles:` — you opt in only to what you actually use.

## Prerequisites

- Docker + Compose v2.
- A domain on Cloudflare (free plan is fine).
- A **remotely-managed** Cloudflare Tunnel (Zero Trust → Networks → Tunnels → Create → "Cloudflared"). Token-based. Public Hostnames live in the dashboard, not in a local file.
- A scoped Cloudflare API token: `Zone:DNS:Edit` + `Zone:Zone:Read` on your zone. Used by Traefik for the ACME DNS-01 challenge.

## Quick start

```bash
git clone https://github.com/AndrewDemsDS/baseplate.git
cd baseplate
cp .env.example .env
$EDITOR .env                          # fill in values
docker compose --profile nextcloud --profile vaultwarden up -d
```

Add Public Hostnames in the Cloudflare Zero Trust dashboard:

| Hostname | Service URL | Notes |
| --- | --- | --- |
| `cloud.example.com` | `http://nextcloud:80` | direct to container |
| `vault.example.com` | `http://vaultwarden:80` | direct to container |
| `paperless.example.com` | `http://paperless:8000` | direct to container |
| `traefik.example.com` | `http://traefik:80` | optional dashboard, IP-allowlisted |

The Tunnel and your containers share the `proxy` Docker network, so the hostnames resolve.

## Architecture

```
                 ┌─────────────────────────────────────────────────────────┐
                 │                Cloudflare edge (anycast)                │
                 └───────────────────────────┬─────────────────────────────┘
                                             │ outbound-only QUIC
                                             ▼
                 ┌────────────── your host ────────────────┐
                 │   cloudflared ────► proxy network       │
                 │                       │                 │
                 │                       ▼                 │
                 │   traefik (TLS via Cloudflare DNS-01)   │
                 │                       │                 │
                 │           ┌───────────┴──────────────┐  │
                 │           ▼              ▼           ▼  │
                 │       nextcloud     vaultwarden   paperless  ...
                 │           │                                 │
                 │       internal network (no public exposure) │
                 │           │                                 │
                 │       nextcloud_db                          │
                 └─────────────────────────────────────────────┘
```

Two networks:

- `proxy` — services that need to be reachable by Traefik or by the Tunnel.
- `baseplate_internal` — databases and other backends. `internal: true` means no host bridge, no internet, just service-to-service traffic on the host.

## Gotchas worth knowing

A list of things that bit me during setup. Some apply specifically to Cloudflare Tunnel + Traefik; some are general homelab traps.

### 1. Remotely-managed vs locally-managed tunnels

When you create a Tunnel in the Cloudflare Zero Trust dashboard, you can pick **Cloudflared** (locally-managed — ingress in a local YAML file) or you can let the dashboard manage it (remotely-managed — ingress in the UI).

`baseplate` assumes **remotely-managed**. If you used `cloudflared tunnel create` from the CLI without the dashboard, your tunnel may be locally-managed and the dashboard will tell you "Routes are configured via the local configuration file." In that case: delete and recreate, picking the dashboard-managed flow, or move ingress into a YAML file mounted into the `cloudflared` container.

### 2. HTTP vs HTTPS in the Public Hostname

When you add a Public Hostname in Zero Trust, the **Type** field decides whether the tunnel makes an HTTP or HTTPS request to your origin. The default is HTTPS — and if your container speaks HTTP, you get a `502 Bad Gateway` with `tls: first record does not look like a TLS handshake` in the `cloudflared` logs.

Set Type to **HTTP** for plain-HTTP origins (which is most containers behind Traefik, since Traefik is the one terminating TLS — your container itself doesn't need to).

### 3. SPF can only appear once

If you also set up Cloudflare Email Routing for your domain, it creates an `MX` record + an SPF `TXT` record like `v=spf1 include:_spf.mx.cloudflare.net ~all`. If you later add a transactional mail provider (Resend, Mailgun, SES, ...) and they tell you to add an SPF record, **do not add a second one**. A domain can only have one effective SPF record. Merge them:

```
v=spf1 include:_spf.mx.cloudflare.net include:amazonses.com ~all
```

DKIM records use distinct selectors and can coexist freely. DMARC is a single record at `_dmarc.example.com`.

### 4. Cloudflare DDNS does not coexist with Cloudflare Tunnel for the same hostname

If you proxy `cloud.example.com` through the Tunnel, don't also point a DDNS A-record at it. Pick one origin model per hostname.

### 5. Vaultwarden's `ADMIN_TOKEN` should be Argon2-hashed

Vaultwarden accepts a plaintext `ADMIN_TOKEN` and will start with one, but it logs a startup warning and is genuinely less safe (the token sits in your compose env vars). Generate an Argon2 hash:

```bash
docker run --rm vaultwarden/server:latest vaultwarden hash
```

Paste the resulting `$argon2id$v=19$...` string as the `ADMIN_TOKEN` value.

### 6. MariaDB password env vars only matter on first run

If you bring up `nextcloud_db` once and later change `MYSQL_PASSWORD` in your `.env`, the container won't notice — its volume already has the user with the old password. You have to `ALTER USER` from inside the running container, then update Nextcloud's `config.php` to match. **Set strong distinct passwords from the start.**

### 7. Don't reuse passwords across services

Yes, even on a homelab. If one container is exploited and your DB credentials are reused as your Vaultwarden admin token... you don't want that.

## Customization

Each service lives in `compose.yml` and is opt-in via a `profiles:` entry. To add your own app:

```yaml
my-app:
  image: ghcr.io/example/my-app:latest
  restart: unless-stopped
  profiles: ["my-app"]
  networks: [proxy]
  labels:
    traefik.enable: "true"
    traefik.http.routers.my-app.rule: "Host(`my-app.${DOMAIN}`)"
    traefik.http.routers.my-app.entrypoints: "websecure"
    traefik.http.routers.my-app.tls.certresolver: "cloudflare"
    traefik.http.services.my-app.loadbalancer.server.port: "8080"
```

Then add a Public Hostname in the dashboard pointing `my-app.example.com` at `http://my-app:8080`, and:

```bash
docker compose --profile nextcloud --profile vaultwarden --profile my-app up -d
```

## License

MIT. See [LICENSE](./LICENSE).
