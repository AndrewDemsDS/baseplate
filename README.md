# baseplate

An opinionated, single-host self-hosted stack. Drop a `.env`, pick the services you want with compose `profiles`, and you have a Traefik-fronted homelab reachable from anywhere via a Cloudflare Tunnel. No port forwarding. No public IP. No Dynamic DNS.

Tested on a Raspberry Pi 5 running Debian 13. Works on any Docker host (mini-PC, NAS, VPS).

## What you get

| Service | Why it's here |
| --- | --- |
| **Traefik v3** | Reverse proxy, automatic Let's Encrypt certs via Cloudflare DNS-01 |
| **Cloudflare Tunnel** | Outbound-only connection to Cloudflare's edge. Your services on the internet without exposing any router ports. |
| **Watchtower** | Nightly image updates with cleanup |
| **Nextcloud** *(opt-in)* | Files + calendar + contacts + Office collab. Includes a Redis cache for file locking and session storage. |
| **Vaultwarden** *(opt-in)* | Bitwarden-compatible password manager |
| **Paperless-ngx** *(opt-in)* | Searchable document archive |
| **Home Assistant** *(opt-in)* | Smart-home hub. Runs in host network mode for full LAN device discovery. |
| **Mosquitto** *(opt-in)* | MQTT broker for home automation |
| **WireGuard** *(opt-in)* | VPN back into your LAN |
| **Cloudflare DDNS** *(opt-in)* | Classic A-record updates, if you want a direct-IP path alongside the tunnel |

The apps all sit behind compose `profiles:`. You opt in to what you use.

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

Available profiles: `nextcloud`, `vaultwarden`, `paperless`, `assistant`, `mqtt`, `wireguard`, `ddns`. Combine the ones you want.

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

- `proxy`: services that need to be reachable by Traefik or by the Tunnel.
- `baseplate_internal`: databases and other backends. `internal: true` means no host bridge, no internet, just service-to-service traffic on the host.

## Gotchas worth knowing

Things that bit me during setup. Some apply to Cloudflare Tunnel + Traefik. Some are general homelab traps.

### 1. Remotely-managed vs locally-managed tunnels

When you create a Tunnel in the Cloudflare Zero Trust dashboard, you can pick **Cloudflared** (locally-managed: ingress in a local YAML file) or you can let the dashboard manage it (remotely-managed: ingress in the UI).

`baseplate` assumes **remotely-managed**. If you used `cloudflared tunnel create` from the CLI without the dashboard, your tunnel may be locally-managed and the dashboard will tell you "Routes are configured via the local configuration file." In that case: delete and recreate, picking the dashboard-managed flow, or move ingress into a YAML file mounted into the `cloudflared` container.

### 2. HTTP vs HTTPS in the Public Hostname

When you add a Public Hostname in Zero Trust, the **Type** field decides whether the tunnel makes an HTTP or HTTPS request to your origin. The default is HTTPS. If your container speaks HTTP, you get a `502 Bad Gateway` with `tls: first record does not look like a TLS handshake` in the `cloudflared` logs.

Set Type to **HTTP** for plain-HTTP origins. Most containers behind Traefik fall into that bucket, since Traefik terminates TLS for them and the container itself sees plain HTTP.

### 3. SPF can only appear once

If you also set up Cloudflare Email Routing for your domain, it creates an `MX` record plus an SPF `TXT` record like `v=spf1 include:_spf.mx.cloudflare.net ~all`. If you later add a transactional mail provider (Resend, Mailgun, SES, ...) and they tell you to add an SPF record, **do not add a second one**. A domain can only have one effective SPF record. Merge them:

```
v=spf1 include:_spf.mx.cloudflare.net include:amazonses.com ~all
```

DKIM records use distinct selectors and coexist without merging. DMARC is a single record at `_dmarc.example.com`.

### 4. Cloudflare DDNS does not coexist with Cloudflare Tunnel for the same hostname

If you proxy `cloud.example.com` through the Tunnel, don't also point a DDNS A-record at it. Pick one origin model per hostname.

### 5. Vaultwarden's `ADMIN_TOKEN` should be Argon2-hashed

Vaultwarden accepts a plaintext `ADMIN_TOKEN` and will start with one, but it logs a startup warning, and the token then sits in your compose env vars in cleartext. Generate an Argon2 hash:

```bash
docker run --rm vaultwarden/server:latest vaultwarden hash
```

Paste the resulting `$argon2id$v=19$...` string as the `ADMIN_TOKEN` value.

### 6. MariaDB password env vars only matter on first run

If you bring up `nextcloud_db` once and later change `MYSQL_PASSWORD` in your `.env`, the container won't notice. Its volume already has the user with the old password baked in. You have to `ALTER USER` from inside the running container, then update Nextcloud's `config.php` to match. Set strong distinct passwords from the start.

### 7. Home Assistant runs in host network mode

The `assistant` profile uses `network_mode: host`, which means Home Assistant shares the host's network stack: it can do mDNS/SSDP discovery, talk to HomeKit and Matter, and reach any device on your LAN without bridge-network NAT.

Trade-off: it sits outside the `proxy` network, so Traefik can't reverse-proxy it via Docker labels. Reach it on `http://<host-ip>:8123` directly, or add a Public Hostname in the Cloudflare Tunnel dashboard pointing at `http://host.docker.internal:8123` (on Linux you may need to add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `cloudflared` service).

### 8. Nextcloud needs Redis config in `config.php` too

Setting `REDIS_HOST` as an env var on the Nextcloud container is not enough on its own. After first start, edit `config/config.php` inside the container and add the `memcache.local`, `memcache.locking`, and `redis` entries. Otherwise Nextcloud will keep using file locking and you'll see "Transactional file locking is disabled" warnings in admin overview.

### 9. Don't reuse passwords across services

Yes, even on a homelab. If one container is exploited and your DB credentials are reused as your Vaultwarden admin token... you don't want that.

## Customization

Each service lives in `compose.yml` and opts in via a `profiles:` entry. To add your own app:

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
