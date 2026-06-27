# Security

## Reporting a vulnerability

Email the maintainer at you@example.com with details and steps to reproduce. Do
not open a public issue for a security problem. Expect an acknowledgement within a
few days.

## What this template does

- Every service runs with `no-new-privileges`.
- Databases sit on internal-only Docker networks with no published ports.
- Watchtower reaches the Docker API through a read-only socket-proxy, never the raw
  socket. Traefik mounts no socket at all.
- Routes are LAN-only by default (`ipAllowList` for private ranges) and carry HSTS,
  nosniff, and frame-deny headers. Admin UIs without their own login get basic auth.
- A weekly Trivy scan flags HIGH/CRITICAL CVEs in the images you run.

## What you still own

- Set strong, unique secrets in `.env`. Hash the Vaultwarden admin token.
- Keep `.env`, `traefik/certs/`, and `mosquitto/passwd` out of git (the `.gitignore`
  already covers them).
- Decide your exposure. The default is LAN-only; the `tunnel` and `wireguard`
  profiles put services within reach from outside, so enable them deliberately.
- Update the pinned stateful apps yourself; Watchtower will not.
