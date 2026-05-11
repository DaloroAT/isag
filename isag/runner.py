"""Wrap `docker compose` invocations."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _base(compose_file: Path) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file)]


def build_command(compose_file: Path) -> list[str]:
    return _base(compose_file) + ["build"]


def run_command(compose_file: Path, service: str, *, rebuild: bool) -> list[str]:
    cmd = _base(compose_file) + ["run", "--rm"]
    if rebuild:
        cmd += ["--build"]
    cmd += [service]
    return cmd


def build(compose_file: Path) -> int:
    return subprocess.call(build_command(compose_file))


def run(compose_file: Path, service: str, *, rebuild: bool) -> int:
    return subprocess.call(run_command(compose_file, service, rebuild=rebuild))