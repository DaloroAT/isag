"""Click CLI: init / run."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from pydantic import ValidationError

from isag.templates.compose import render_compose, render_domains
from isag.templates.dockerfile import render_dockerfile, render_dockerignore
from isag.templates.entrypoint import render_entrypoint
from isag.models import SandboxConfig, to_user_path
from isag.runner import build as compose_build, run as compose_run
from isag.templates.config import starter_template
from isag.utils import yaml_id

_config_opt = click.option(
    "-c", "--config", "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("isag.yaml"),
    show_default=True,
    help="Path to the isag YAML.",
)


def _user_cache_root() -> Path:
    """User cache root for sandbox artifacts.

    Linux and WSL2 (which reports as `linux` and uses the XDG path) are the
    tested paths. The darwin/win32 branches are best-effort: Docker
    Desktop / Rancher Desktop on macOS and native-Windows Docker should
    work but aren't exercised in CI.
    """
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
        return Path(base) / "isag"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "isag"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "isag" / "Cache"
    raise RuntimeError(f"Unsupported platform: {sys.platform!r}")


def _cache_dir_for(yaml_path: Path) -> Path:
    """Cache slot for one isag.yaml's generated artifacts.

    Slot name is `<parent_dir>_<path_hash>`: the parent dir name (typically
    the project name) for browseability, plus a hash of the absolute yaml
    path for disambiguation when two projects share a parent name. Sibling
    yamls (isag.dev.yaml, isag.prod.yaml) get separate slots; moving
    a yaml gives a fresh slot. Use `meta.json` inside each slot to identify
    it precisely.
    """
    resolved = yaml_path.resolve()
    digest = yaml_id(resolved)
    label = resolved.parent.name or "root"
    return _user_cache_root() / f"{label}_{digest}"


def _load(path: Path) -> SandboxConfig:
    try:
        return SandboxConfig.from_yaml(path)
    except FileNotFoundError:
        raise click.ClickException(f"Config not found: {path}")
    except ValidationError as e:
        raise click.ClickException(f"Invalid config:\n{e}")


def _check_project_exists(cfg: SandboxConfig) -> None:
    """The project directory is user input; we never auto-create it."""
    project = cfg.project_root
    if not project.is_dir():
        raise click.ClickException(
            f"Project directory does not exist on host: {project}\n"
            f"  (set as project in isag.yaml — pointed at your code)"
        )


def _check_exclude_paths(cfg: SandboxConfig) -> None:
    """Fail fast if any excluded path is missing or wrong type on host.

    Files must exist as regular files (or symlinks to files); folders must
    exist as directories. Bind-mounting a directory over a file or vice
    versa fails with ENOTDIR at docker compose up — better to catch it now
    with a clear message.
    """
    if cfg.exclude is None:
        return
    for p in cfg.exclude.files:
        resolved = cfg.resolve_path(p)
        if not resolved.exists():
            raise click.ClickException(
                f"exclude.files: path does not exist on host: {resolved}"
            )
        if not resolved.is_file():
            raise click.ClickException(
                f"exclude.files: not a file on host: {resolved}\n"
                f"  (move it under exclude.folders if it's a directory)"
            )
    for p in cfg.exclude.folders:
        resolved = cfg.resolve_path(p)
        if not resolved.exists():
            raise click.ClickException(
                f"exclude.folders: path does not exist on host: {resolved}"
            )
        if not resolved.is_dir():
            raise click.ClickException(
                f"exclude.folders: not a directory on host: {resolved}\n"
                f"  (move it under exclude.files if it's a file)"
            )


def _ensure_host_dirs(cfg: SandboxConfig) -> None:
    """Pre-create host-side bind-mount sources for state directories.

    Excludes the project directory (must be user-provided).
    """
    paths: set[Path] = {
        cfg.resolve_path(cfg.agent.host_home) / f".{cfg.agent.vendor.value}",
        cfg.resolve_path(cfg.container.host_cache_dir),
    }
    for m in cfg.mounts or []:
        paths.add(cfg.resolve_path(m.host))
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _materialize(cfg: SandboxConfig, yaml_path: Path) -> Path:
    outdir = _cache_dir_for(yaml_path)
    outdir.mkdir(parents=True, exist_ok=True)
    _ensure_host_dirs(cfg)

    (outdir / "Dockerfile").write_text(render_dockerfile(cfg))
    (outdir / ".dockerignore").write_text(render_dockerignore())
    (outdir / "entrypoint.sh").write_text(render_entrypoint(cfg))
    if cfg.limit_network is not None:
        (outdir / "allowed-domains.txt").write_text(render_domains(cfg))
    compose = outdir / "docker-compose.yaml"
    compose.write_text(render_compose(cfg, outdir=outdir, dockerfile="Dockerfile", yaml_path=yaml_path))

    # Inspection aids: which source yaml + project this slot came from, and
    # the literal yaml content that produced these artifacts. `meta.json`
    # answers "what is this slot for?"; `applied.yaml` answers "what config
    # made this image?" (diff against current isag.yaml to see drift).
    meta = {
        "yaml_path": str(yaml_path.resolve()),
        "project_path": str(cfg.project_root),
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    (outdir / "applied.yaml").write_text(yaml_path.read_text())

    return compose


def _resolve_init_project(project: str | None) -> str:
    """Pick the literal string for `project.host` in the new YAML.

    No `--project` → `.` (committable; resolves against the YAML dir at
    runtime). Explicit `--project <path>` → absolute, in `~/`-form when
    under home.
    """
    if project is None:
        return "."
    p = Path(project).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    return to_user_path(p)


@click.group()
@click.version_option()
def cli():
    """Manage strict containerized agent sandboxes."""


@cli.command()
@click.option(
    "-o", "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("isag.yaml"), show_default=True,
)
@click.option("--force", is_flag=True, help="Overwrite if file exists.")
@click.option(
    "--extended", is_flag=True,
    help="Include ML/data presets (Stack Overflow, PyTorch, CUDA, conda, "
         "HuggingFace) plus example mounts, env, and env_files.",
)
@click.option(
    "--gpu", is_flag=True,
    help="Pre-fill a GPU/CUDA starter: nvidia/cuda Ubuntu base image, "
         "gpu: true in container, and NVIDIA/PyTorch domains in the allowlist.",
)
@click.option(
    "--project", "project_arg",
    type=str, default=None,
    help="Host path of the project to mount. Defaults to the current directory.",
)
def init(output: Path, force: bool, extended: bool, gpu: bool, project_arg: str | None):
    """Generate a starter isag.yaml."""
    if output.exists() and not force:
        raise click.ClickException(f"{output} exists; pass --force to overwrite.")
    project = _resolve_init_project(project_arg)
    output.write_text(starter_template(extended=extended, gpu=gpu, project=project))
    click.echo(f"Wrote {output} (project: {project})")


@cli.command()
@_config_opt
@click.option("--build-only", is_flag=True, help="Build the image but don't run.")
def run(config_path: Path, build_only: bool):
    """Materialize artifacts, build the image, and run the sandbox.

    Build always runs so the container reflects the current YAML; pass
    `--build-only` to stop after the build. Artifacts go to a per-yaml
    cache slot under $XDG_CACHE_HOME (default ~/.cache/isag/).
    """
    cfg = _load(config_path)
    _check_project_exists(cfg)
    _check_exclude_paths(cfg)
    compose = _materialize(cfg, config_path)
    click.echo(f"isag: artifacts at {compose.parent}")

    if build_only:
        sys.exit(compose_build(compose))
    sys.exit(compose_run(compose, cfg.container.name, rebuild=True))


if __name__ == "__main__":
    cli()