"""Generate a docker-compose.yaml from a SandboxConfig."""
from __future__ import annotations

from pathlib import Path

from isag.models import AgentConfig, Mount, SandboxConfig, Vendor, VENDOR_HOME_ENV
from isag.utils import yaml_id

GENERATED_HEADER = "# Auto-generated. Do not edit by hand.\n"

_YOLO_FLAGS: dict[Vendor, str] = {
    Vendor.CODEX: "--dangerously-bypass-approvals-and-sandbox",
    Vendor.CLAUDE: "--dangerously-skip-permissions",
}


def _agent_command(agent: AgentConfig) -> list[str]:
    cmd = [agent.vendor.value]
    if agent.yolo_mode:
        cmd.append(_YOLO_FLAGS[agent.vendor])
    return cmd


def _volume_entry(m: Mount, cfg: SandboxConfig) -> dict:
    entry: dict = {
        "type": "bind",
        "source": str(cfg.resolve_path(m.host)),
        "target": str(m.container),
    }
    if m.mode == "ro":
        entry["read_only"] = True
    return entry


def _resolve_masks(cfg: SandboxConfig) -> list[dict]:
    """Translate `exclude` host paths into mask volume entries.

    For each host path, find every mount whose source contains it and emit
    a mask at the corresponding container path. Files get a `/dev/null`
    bind (kernel-empty char device); folders get an anonymous Docker volume
    (auto-created empty dir, auto-cleaned by `compose run --rm`). The agent
    can't unmount either (no `CAP_SYS_ADMIN` in the container).
    """
    if cfg.exclude is None:
        return []

    pairs: list[tuple[Path, Path]] = [(cfg.project_root, cfg.project.container)]
    for m in cfg.mounts or []:
        pairs.append((cfg.resolve_path(m.host), m.container))

    def _targets_for(host: Path) -> list[str]:
        out: list[str] = []
        for src, dst in pairs:
            try:
                rel = host.relative_to(src)
            except ValueError:
                continue
            out.append(str(dst / rel) if str(rel) != "." else str(dst))
        return out

    masks: list[dict] = []
    seen: set[str] = set()
    for f in cfg.exclude.files:
        host = cfg.resolve_path(f)
        for target in _targets_for(host):
            if target in seen:
                continue
            seen.add(target)
            masks.append({
                "type": "bind",
                "source": "/dev/null",
                "target": target,
                "read_only": True,
            })
    for d in cfg.exclude.folders:
        host = cfg.resolve_path(d)
        for target in _targets_for(host):
            if target in seen:
                continue
            seen.add(target)
            # Anonymous volume: target only, no source. Docker creates an
            # empty backing dir at runtime and removes it on `--rm` exit.
            masks.append({"type": "volume", "target": target})
    return masks


def render_domains(config: SandboxConfig) -> str:
    assert config.limit_network is not None, "render_domains requires limit_network"
    seen: set[str] = set()
    out: list[str] = []
    for d in (*config.agent.required_domains, *config.limit_network.domains):
        if d not in seen:
            seen.add(d)
            out.append(d)
    return "\n".join(out) + "\n"


def render_compose(
    config: SandboxConfig,
    *,
    outdir: Path,
    yaml_path: Path,
    dockerfile: str = "Dockerfile",
    domains_file: str = "allowed-domains.txt",
) -> str:
    import yaml

    cache_target = config.container.cache_target
    # Vendor segment lives on both sides of the mount: flipping `agent.vendor`
    # in the YAML changes the host subdirectory and the in-container target
    # in lockstep, giving true cross-vendor isolation under one host_home.
    vendor_segment = f".{config.agent.vendor.value}"
    vendor_home_source = config.resolve_path(config.agent.host_home) / vendor_segment
    vendor_home_target = f"{config.container.home}/{vendor_segment}"

    volumes: list[dict] = [_volume_entry(config.project, config)]
    volumes.append({
        "type": "bind",
        "source": str(vendor_home_source),
        "target": vendor_home_target,
    })
    volumes.append({
        "type": "bind",
        "source": str(config.resolve_path(config.container.host_cache_dir)),
        "target": cache_target,
    })
    if config.limit_network is not None:
        domains_src = (Path(outdir) / domains_file).resolve()
        volumes.append({
            "type": "bind",
            "source": str(domains_src),
            "target": "/etc/isag/allowed-domains.txt",
            "read_only": True,
        })
    volumes.extend(_volume_entry(m, config) for m in (config.mounts or []))

    # If the source yaml lives under the project tree, overlay it as
    # read-only over its own path inside the container. The agent can read
    # the policy that constrains it but cannot edit it (kernel-enforced via
    # the bind mount; ownership and mode bits are bypassed). When the yaml
    # lives outside the project, the agent never sees it — no overlay needed.
    yaml_abs = yaml_path.resolve()
    try:
        rel = yaml_abs.relative_to(config.project_root)
    except ValueError:
        pass
    else:
        volumes.append({
            "type": "bind",
            "source": str(yaml_abs),
            "target": str(config.project.container / rel),
            "read_only": True,
        })

    # Append `exclude` masks last so they overlay anything beneath them.
    # Compose orders mounts by target depth before applying, but for equal
    # depth the list order wins — putting masks after the base mounts keeps
    # user intent ("hide this path") authoritative.
    volumes.extend(_resolve_masks(config))

    # Layer order: infrastructure defaults, then vendor requirements, then
    # user overrides. User has the final say on every key.
    environment = {
        "NPM_CONFIG_CACHE": f"{cache_target}/npm",
        "AGENT_VENDOR": config.agent.vendor.value,
        # Relocate the vendor's entire config tree (incl. previously-loose
        # dotfile sibling, e.g. ~/.claude.json) inside the bind-mounted dir.
        VENDOR_HOME_ENV[config.agent.vendor]: vendor_home_target,
    }
    if config.limit_network is not None:
        environment["ISAG_RESOLVERS"] = " ".join(config.limit_network.dns)
    environment.update(config.agent.required_env)
    if config.container.gpu:
        environment["NVIDIA_VISIBLE_DEVICES"] = "all"
        environment["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
    if config.env is not None:
        environment.update(config.env)

    service: dict = {
        "build": {
            "context": ".",
            "dockerfile": dockerfile,
            "args": {
                "BASE_IMAGE": config.container.base_image,
                "PYTHON_VERSION": config.container.python,
                "AGENT_PACKAGE": config.agent.package,
                "AGENT_CLI_VERSION": config.agent.cli_version,
                "USER_NAME": config.container.user,
            },
        },
        "image": f"{config.container.name}:{yaml_id(yaml_path)}",
        "container_name": config.container.name,
        "init": True,
        "tty": True,
        "stdin_open": True,
        "working_dir": str(config.project.container),
        "volumes": volumes,
        "environment": environment,
        "command": _agent_command(config.agent),
    }

    if config.limit_network is not None:
        service["cap_add"] = ["NET_ADMIN"]
        service["dns"] = list(config.limit_network.dns)

    if config.container.external_networks:
        # Declaring `networks:` on a service stops compose's auto-attach to
        # the project default network. Append "default" so cross-service
        # communication within this compose project keeps working.
        service["networks"] = [*config.container.external_networks, "default"]

    if config.container.gpu:
        service["deploy"] = {
            "resources": {
                "reservations": {
                    "devices": [{
                        "driver": "nvidia",
                        "count": "all",
                        "capabilities": ["gpu"],
                    }]
                }
            }
        }

    if config.env_files:
        service["env_file"] = [str(config.resolve_path(p)) for p in config.env_files]

    doc: dict = {"services": {config.container.name: service}}
    if config.container.external_networks:
        doc["networks"] = {
            name: {"external": True}
            for name in config.container.external_networks
        }
    return GENERATED_HEADER + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)