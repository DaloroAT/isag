"""Pydantic models for the sandbox configuration."""
from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    field_validator,
    model_serializer,
    model_validator,
)


class Vendor(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"


VENDOR_PACKAGES: dict[Vendor, str] = {
    Vendor.CODEX: "@openai/codex",
    Vendor.CLAUDE: "@anthropic-ai/claude-code",
}

VENDOR_DOMAINS: dict[Vendor, tuple[str, ...]] = {
    Vendor.CODEX: ("api.openai.com", "chatgpt.com", "auth.openai.com"),
    # statsig.* is operational telemetry, disabled below via VENDOR_ENV.
    Vendor.CLAUDE: ("api.anthropic.com",),
}

VENDOR_ENV: dict[Vendor, dict[str, str]] = {
    Vendor.CODEX: {},
    # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC stops statsig + sentry + bug
    # reports; DISABLE_TELEMETRY is a belt-and-suspenders for the same.
    Vendor.CLAUDE: {
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    },
}

# Each vendor exposes one env var that relocates its entire config tree.
# Setting it to the vendor mount target makes both the directory contents
# and the formerly-loose dotfile sibling (e.g. ~/.claude.json) live inside
# the same bind-mounted dir, so everything persists across runs.
VENDOR_HOME_ENV: dict[Vendor, str] = {
    Vendor.CODEX: "CODEX_HOME",
    Vendor.CLAUDE: "CLAUDE_CONFIG_DIR",
}


def expand(p: Path | str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


def to_user_path(p: Path | str) -> str:
    """Render an absolute path as ~/relative when it lives under the user's
    home directory; absolute string otherwise. Idempotent: passing the result
    back through expanduser() yields the original absolute path."""
    abspath = Path(p).expanduser().resolve()
    home = Path.home().resolve()
    try:
        rel = abspath.relative_to(home)
    except ValueError:
        return str(abspath)
    return "~" if str(rel) == "." else f"~/{rel}"


# --- Mount -------------------------------------------------------------------

class Mount(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: Path
    container: Path
    mode: Literal["ro", "rw"]

    @model_validator(mode="before")
    @classmethod
    def _parse_shorthand(cls, v):
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) != 3:
                raise ValueError(f"mount must be 'host:container:mode', got {v!r}")
            host, container, mode = parts
            return {"host": host, "container": container, "mode": mode}
        return v

    @field_validator("container")
    @classmethod
    def _container_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(f"container path must be absolute: {v}")
        return v

    @model_serializer
    def _to_shorthand(self) -> str:
        return f"{self.host}:{self.container}:{self.mode}"


# --- Exclude -----------------------------------------------------------------

class ExcludeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    files: list[Path] = []
    folders: list[Path] = []

    @model_validator(mode="after")
    def _no_overlap(self) -> "ExcludeConfig":
        # Same path under both buckets is ambiguous: the file/folder bucket
        # decides the mask kind (/dev/null vs anonymous volume), so reject
        # rather than guess. Type-vs-bucket mismatches against the host fs
        # are caught later in main.py where filesystem access is available.
        files = {str(p) for p in self.files}
        folders = {str(p) for p in self.folders}
        overlap = files & folders
        if overlap:
            raise ValueError(
                f"exclude: path(s) listed in both files and folders: {sorted(overlap)}"
            )
        return self


# --- Sub-configs -------------------------------------------------------------

class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    vendor: Vendor
    yolo_mode: bool
    host_home: Path
    cli_version: str

    @field_validator("cli_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if v == "latest":
            return v
        if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", v):
            return v
        if re.fullmatch(r"[a-z][a-z0-9-]*", v):
            return v
        raise ValueError(
            f"cli_version must be 'latest', semver, or dist-tag; got {v!r}"
        )

    @property
    def package(self) -> str:
        return VENDOR_PACKAGES[self.vendor]

    @property
    def required_domains(self) -> tuple[str, ...]:
        return VENDOR_DOMAINS[self.vendor]

    @property
    def required_env(self) -> dict[str, str]:
        return dict(VENDOR_ENV[self.vendor])


class ContainerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    base_image: str
    python: str
    user: str
    host_cache_dir: Path
    extra_packages: list[str]
    gpu: bool
    external_networks: list[str]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", v):
            raise ValueError(
                f"container.name must match Docker naming rules, got {v!r}"
            )
        return v

    @field_validator("extra_packages")
    @classmethod
    def _validate_extra_packages(cls, v: list[str]) -> list[str]:
        # Apt package name regex per Debian Policy §5.6.1: lowercase letters,
        # digits, plus, minus, and dots; must start with a letter or digit.
        # Strict validation here is non-negotiable — these names land in a
        # `RUN apt-get install` line in the Dockerfile, so an unvalidated
        # entry like `nano; rm -rf /` would execute at build time.
        pat = re.compile(r"^[a-z0-9][a-z0-9+\-.]*$")
        for pkg in v:
            if not pat.fullmatch(pkg):
                raise ValueError(
                    f"container.extra_packages: invalid apt package name {pkg!r}"
                )
        return v

    @field_validator("external_networks")
    @classmethod
    def _validate_external_networks(cls, v: list[str]) -> list[str]:
        # Names land verbatim as top-level keys in docker-compose.yaml under
        # `networks:`; strict validation prevents YAML injection (a name
        # containing "x:\n  driver: foo" would forge sibling keys).
        pat = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
        seen: set[str] = set()
        for name in v:
            if not pat.fullmatch(name):
                raise ValueError(
                    f"container.external_networks: invalid network name {name!r}"
                )
            if name in seen:
                raise ValueError(
                    f"container.external_networks: duplicate entry {name!r}"
                )
            seen.add(name)
        return v

    @field_validator("python")
    @classmethod
    def _validate_python(cls, v: str) -> str:
        if not re.fullmatch(r"\d+\.\d+", v):
            raise ValueError(f"python_version must be 'X.Y' (e.g. '3.12'), got {v!r}")
        return v

    @field_validator("user")
    @classmethod
    def _validate_user(cls, v: str) -> str:
        if v == "root":
            raise ValueError("container.user must not be 'root'")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", v):
            raise ValueError(
                f"container.user must be a valid POSIX username, got {v!r}"
            )
        return v

    @property
    def home(self) -> str:
        return f"/home/{self.user}"

    @property
    def cache_target(self) -> str:
        return f"{self.home}/.cache"


_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class NetworkConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    dns: list[str]
    domains: list[str]

    @field_validator("dns")
    @classmethod
    def _validate_dns(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("limit_network.dns must contain at least one resolver")
        for ip in v:
            if not _IPV4_RE.match(ip):
                raise ValueError(f"invalid DNS server: {ip!r}")
        return v

    @field_validator("domains")
    @classmethod
    def _validate_domains(cls, v: list[str]) -> list[str]:
        for d in v:
            if not _DOMAIN_RE.match(d):
                raise ValueError(f"invalid domain: {d!r}")
        return v


# --- Root --------------------------------------------------------------------

class SandboxConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    project: Mount
    agent: AgentConfig
    container: ContainerConfig
    limit_network: NetworkConfig | None
    mounts: list[Mount] | None
    exclude: ExcludeConfig | None
    env: dict[str, str] | None
    env_files: list[Path] | None

    # Anchor for resolving relative host paths. Set by `from_yaml` to the
    # absolute config-file path; falls back to CWD for direct construction.
    # Private — `SandboxConfig` stays file-agnostic in its public API.
    _anchor_path: Path = PrivateAttr(default_factory=Path.cwd)

    @field_validator("env", mode="before")
    @classmethod
    def _stringify_env(cls, v):
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items()}
        return v

    @model_validator(mode="after")
    def _check_unique_targets(self) -> "SandboxConfig":
        mounts = self.mounts or []
        targets = [self.project.container] + [m.container for m in mounts]
        seen: set[Path] = set()
        for t in targets:
            if t in seen:
                raise ValueError(f"duplicate mount target: {t}")
            seen.add(t)
        return self

    @property
    def project_root(self) -> Path:
        """Absolute host path of the project mount source.

        `~`/`$VAR` expand first. If still relative, anchor at the config
        file's directory (or CWD when constructed without `from_yaml`).
        """
        p = Path(os.path.expandvars(os.path.expanduser(str(self.project.host))))
        if p.is_absolute():
            return p.resolve()
        return (self._anchor_path.parent / p).resolve()

    def resolve_path(self, p: Path | str) -> Path:
        """Resolve any non-project host path.

        `~`/`$VAR` expand first. Already-absolute paths return as-is.
        Relative paths anchor at the project root — not at the config
        file's directory. The project IS the root of the sandbox config.
        """
        expanded = Path(os.path.expandvars(os.path.expanduser(str(p))))
        if expanded.is_absolute():
            return expanded.resolve()
        return (self.project_root / expanded).resolve()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SandboxConfig":
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"config root must be a mapping, got {type(data).__name__}")
        cfg = cls.model_validate(data)
        # Bypass frozen=True for the private anchor.
        object.__setattr__(cfg, "_anchor_path", Path(path).resolve())
        return cfg

    def to_yaml(self) -> str:
        import yaml
        return yaml.safe_dump(
            self.model_dump(mode="json"),
            sort_keys=False,
            default_flow_style=False,
        )