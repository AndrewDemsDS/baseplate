#!/usr/bin/env python3
"""Generate Podman Quadlet units from the compose files.

Compose stays the source of truth. These units are a build artifact: run
`just gen-quadlets` after changing any compose file, and CI fails if the
committed output has drifted.

The units keep `${VAR}` references verbatim, exactly like
traefik/dynamic.yml.template. scripts/install-quadlets.sh substitutes them
at install time.
"""

import os
import shutil
import sys

import yaml

COMPOSE_FILES = ["core.yml", "cloud.yml", "media.yml", "home.yml", "monitoring.yml"]
OUT_DIR = "quadlet"

# config-render exists only because compose has no way to run envsubst before
# the stack starts. install-quadlets.sh does that step directly, so the unit
# would be dead weight -- and its `envsubst '$DOMAIN'` argument is a literal
# that install-time substitution would eat.
SKIP_SERVICES = {"config-render"}

# Podman resolves unqualified names via registries.conf, which varies by host.
# Quadlet units must be explicit or they break on someone else's machine.
DOCKER_IO_PREFIX = "docker.io/"
KNOWN_REGISTRIES = (
    "docker.io/",
    "ghcr.io/",
    "lscr.io/",
    "quay.io/",
    "registry.k8s.io/",
)


def qualify(image):
    if image.startswith(KNOWN_REGISTRIES):
        return image
    return DOCKER_IO_PREFIX + image


def as_env_pairs(env):
    if not env:
        return []
    if isinstance(env, list):
        return [item.split("=", 1) for item in env if "=" in item]
    return [(k, "" if v is None else str(v)) for k, v in env.items()]


def health_lines(hc):
    """Compose healthcheck -> quadlet Health* keys."""
    out = []
    test = hc.get("test")
    if isinstance(test, list):
        # ["CMD", "a", "b"] or ["CMD-SHELL", "..."]
        if test and test[0] in ("CMD", "CMD-SHELL"):
            test = test[1:]
        cmd = " ".join(test)
    else:
        cmd = str(test or "")
    if cmd:
        out.append(("HealthCmd", cmd))
    for compose_key, quadlet_key in (
        ("interval", "HealthInterval"),
        ("timeout", "HealthTimeout"),
        ("retries", "HealthRetries"),
        ("start_period", "HealthStartPeriod"),
    ):
        if compose_key in hc:
            out.append((quadlet_key, str(hc[compose_key])))
    return out


def network_unit_name(compose_net_name):
    return "%s.network" % compose_net_name


def build_unit(name, svc, networks, healthy_deps):
    unit, container, service = [], [], []

    desc = svc.get("container_name", name)
    unit.append(("Description", "baseplate: %s" % desc))

    # depends_on -> ordering. service_healthy needs Notify=healthy on the target,
    # which we add below when a service is named as a healthy dependency.
    deps = svc.get("depends_on") or {}
    dep_names = [
        d
        for d in (list(deps.keys()) if isinstance(deps, dict) else list(deps))
        if d not in SKIP_SERVICES
    ]
    for dep in dep_names:
        unit.append(("After", "%s.service" % dep))
        unit.append(("Requires", "%s.service" % dep))

    # Bind mounts under a host path must wait for that mount at boot. Named
    # volumes are podman-managed and need no guard.
    host_mounts = [
        v.split(":", 1)[0]
        for v in (svc.get("volumes") or [])
        if v.startswith("/") or v.startswith("${")
    ]
    for m in sorted(set(host_mounts)):
        if m.startswith("/"):
            unit.append(("RequiresMountsFor", m))

    container.append(("ContainerName", svc.get("container_name", name)))
    container.append(("Image", qualify(svc["image"])))

    if svc.get("entrypoint"):
        ep = svc["entrypoint"]
        container.append(("Entrypoint", ep if isinstance(ep, str) else " ".join(ep)))

    for k, v in as_env_pairs(svc.get("environment")):
        container.append(("Environment", "%s=%s" % (k, v)))

    for vol in svc.get("volumes") or []:
        # Quadlet requires absolute host paths; compose allows repo-relative ones.
        if vol.startswith("./"):
            vol = "${BASEPLATE_DIR}/" + vol[2:]
        container.append(("Volume", vol))

    if svc.get("network_mode") == "host":
        container.append(("Network", "host"))
    else:
        for net in svc.get("networks") or []:
            container.append(("Network", network_unit_name(networks[net])))

    for port in svc.get("ports") or []:
        container.append(("PublishPort", str(port)))

    for dev in svc.get("devices") or []:
        container.append(("AddDevice", dev))

    for cap in svc.get("cap_add") or []:
        container.append(("AddCapability", cap))
    for cap in svc.get("cap_drop") or []:
        container.append(("DropCapability", cap))

    for host in svc.get("extra_hosts") or []:
        container.append(("AddHost", host))

    sysctls = svc.get("sysctls") or {}
    if isinstance(sysctls, list):
        sysctls = dict(s.split("=", 1) for s in sysctls if "=" in s)
    for key, val in sysctls.items():
        container.append(("Sysctl", "%s=%s" % (key, val)))

    if svc.get("user"):
        container.append(("User", str(svc["user"])))
    elif svc["image"].split(":")[0].endswith("mariadb"):
        # mariadb's gosu privilege-drop hangs under rootful podman+crun, so the
        # container never becomes healthy and everything downstream waits forever.
        # Starting as the mysql uid sidesteps the drop entirely. Docker/runc is
        # unaffected, which is why this has no compose equivalent.
        container.append(("User", "999"))
    if svc.get("read_only"):
        container.append(("ReadOnly", "true"))
    if any("no-new-privileges" in str(o) for o in svc.get("security_opt") or []):
        container.append(("NoNewPrivileges", "true"))

    if svc.get("healthcheck"):
        container.extend(health_lines(svc["healthcheck"]))

    # A dependant declared `condition: service_healthy`. Without Notify=healthy
    # systemd considers the unit started as soon as the container is created,
    # and the dependant races the database.
    if name in healthy_deps:
        container.append(("Notify", "healthy"))
        service.append(("TimeoutStartSec", "300"))

    # Quadlet gained a first-class Memory= key in a later podman than many
    # distros ship (5.4.2 rejects it outright), so route both limits through
    # PodmanArgs -- it works on every version.
    if svc.get("mem_limit"):
        container.append(("PodmanArgs", "--memory=%s" % svc["mem_limit"]))
    if svc.get("cpus"):
        container.append(("PodmanArgs", "--cpus=%s" % svc["cpus"]))

    if svc.get("command"):
        cmd = svc["command"]
        container.append(
            ("Exec", cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd))
        )

    restart = svc.get("restart")
    if restart in ("unless-stopped", "always"):
        service.append(("Restart", "always"))
    elif not restart:
        # one-shot helpers (config-render) must not be restarted
        service.append(("Type", "oneshot"))

    lines = ["[Unit]"]
    lines += ["%s=%s" % kv for kv in unit]
    lines += ["", "[Container]"]
    lines += ["%s=%s" % kv for kv in container]
    if service:
        lines += ["", "[Service]"]
        lines += ["%s=%s" % kv for kv in service]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def build_network_unit(compose_name, cfg):
    lines = [
        "[Unit]",
        "Description=baseplate network: %s" % compose_name,
        "",
        "[Network]",
    ]
    lines.append("NetworkName=%s" % cfg.get("name", compose_name))
    if cfg.get("internal"):
        lines.append("Internal=true")
    # Tunnelled/PPPoE uplinks silently break image pulls at the default 1500.
    # Harmless on a clean 1500 path; see README.
    lines.append("Options=mtu=${PODMAN_NET_MTU}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def main():
    services, networks, net_defs, profiles = {}, {}, {}, {}

    for path in COMPOSE_FILES:
        doc = yaml.safe_load(open(path))
        for net, cfg in (doc.get("networks") or {}).items():
            cfg = cfg or {}
            networks[net] = cfg.get("name", net)
            net_defs[cfg.get("name", net)] = cfg
        for name, svc in (doc.get("services") or {}).items():
            services[name] = svc
            prof = svc.get("profiles") or []
            profiles[name] = prof[0] if prof else "base"

    healthy_deps = set()
    for svc in services.values():
        deps = svc.get("depends_on") or {}
        if isinstance(deps, dict):
            for dep, cond in deps.items():
                if (cond or {}).get("condition") == "service_healthy":
                    healthy_deps.add(dep)

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)

    net_dir = os.path.join(OUT_DIR, "base")
    os.makedirs(net_dir, exist_ok=True)
    for netname, cfg in sorted(net_defs.items()):
        with open(os.path.join(net_dir, "%s.network" % netname), "w") as fh:
            fh.write(build_network_unit(netname, cfg))

    count = 0
    for name, svc in sorted(services.items()):
        if name in SKIP_SERVICES:
            continue
        prof = profiles[name]
        dest = os.path.join(OUT_DIR, prof)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "%s.container" % name), "w") as fh:
            fh.write(build_unit(name, svc, networks, healthy_deps))
        count += 1

    print(
        "generated %d .container units + %d .network units into %s/"
        % (count, len(net_defs), OUT_DIR)
    )
    print("profiles: %s" % ", ".join(sorted(set(profiles.values()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
