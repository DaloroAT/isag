"""Starter templates — built by instantiating models and dumping them."""
from __future__ import annotations

from pathlib import Path

from isag.models import (
    AgentConfig,
    ContainerConfig,
    ExcludeConfig,
    Mount,
    NetworkConfig,
    SandboxConfig,
    Vendor,
)

_HEADER = """\
# Sandbox configuration. Edit values, then `isag run`.
# All fields are required; the YAML file is the single source of truth.
# Paths starting with ~ are expanded at build/run time.
# Vendor-required domains are added automatically based on agent.vendor.

"""

# Minimal allowlist common to almost any project: source forge + python +
# node package indices. Everything else is opt-in via user config.
_BASE_DOMAINS: list[str] = [
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
]

# Convenience preset for ML/data work. Stack Overflow is a common
# documentation lookup target; the rest covers PyTorch CUDA wheels,
# NVIDIA driver/runtime downloads, conda channels, and HuggingFace
# model + dataset hosting.
_EXTENDED_DOMAINS: list[str] = [
    # Stack Overflow.
    "stackoverflow.com",
    "cdn.stackoverflow.co",
    "cdn.sstatic.net",
    "stackexchange.com",
    # PyTorch CUDA wheels.
    "download.pytorch.org",
    # NVIDIA / CUDA.
    "developer.download.nvidia.com",
    "developer.nvidia.com",
    "nvidia.com",
    "www.nvidia.com",
    "api.nvidia.com",
    "ngc.nvidia.com",
    "nvcr.io",
    "us.download.nvidia.com",
    "international.download.nvidia.com",
    # Conda / Anaconda.
    "repo.anaconda.com",
    "conda.anaconda.org",
    "anaconda.org",
    # Hugging Face.
    "huggingface.co",
    "www.huggingface.co",
    "cdn-lfs.hf.co",
    "api-inference.huggingface.co",
]

# Subset of _EXTENDED_DOMAINS auto-added when container.gpu is true so that
# CUDA wheels and NVIDIA driver/tooling downloads aren't blocked by the
# default allowlist.
_GPU_DOMAINS: list[str] = [
    "download.pytorch.org",
    "developer.download.nvidia.com",
    "developer.nvidia.com",
    "nvidia.com",
    "www.nvidia.com",
    "api.nvidia.com",
    "ngc.nvidia.com",
    "nvcr.io",
    "us.download.nvidia.com",
    "international.download.nvidia.com",
]


def _common_skeleton(
    domains: list[str], project: str, extra_packages: list[str], gpu: bool
) -> dict:
    """Fields shared by every starter; caller supplies mounts/env/env_files."""
    return dict(
        project=Mount(
            host=Path(project),
            container=Path("/workspace/project"),
            mode="rw",
        ),
        agent=AgentConfig(
            vendor=Vendor.CLAUDE,
            yolo_mode=True,
            host_home=Path("~/agents"),
            cli_version="latest",
        ),
        container=ContainerConfig(
            name="isag-gpu" if gpu else "isag",
            base_image=(
                "nvidia/cuda:12.8.1-runtime-ubuntu24.04" if gpu else "ubuntu:24.04"
            ),
            python="3.12",
            user="isag",
            host_cache_dir=Path("~/isag-cache"),
            extra_packages=extra_packages,
            gpu=gpu,
            external_networks=[],
        ),
        limit_network=NetworkConfig(
            dns=["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"],
            domains=domains,
        ),
    )


def starter_config(*, extended: bool = False, gpu: bool = False, project: str) -> SandboxConfig:
    domains = list(_BASE_DOMAINS)
    if extended:
        domains += _EXTENDED_DOMAINS
    if gpu:
        # _EXTENDED_DOMAINS already covers _GPU_DOMAINS; dedupe defensively
        # while preserving insertion order.
        domains += _GPU_DOMAINS
    domains = list(dict.fromkeys(domains))

    if extended:
        # Mount examples follow the project's path style: relative for the
        # committable `.` default, absolute (~-form) when an explicit
        # `--project` was given.
        if project == ".":
            example_mounts = [
                Mount(host=Path("./datasets"), container=Path("/workspace/datasets"), mode="ro"),
                Mount(host=Path("./outputs"), container=Path("/workspace/outputs"), mode="rw"),
            ]
        else:
            example_mounts = [
                Mount(host=Path("~/datasets"), container=Path("/workspace/datasets"), mode="ro"),
                Mount(host=Path("~/outputs/my-project"), container=Path("/workspace/outputs"), mode="rw"),
            ]
        return SandboxConfig(
            **_common_skeleton(
                domains=domains,
                project=project,
                extra_packages=["nano", "htop"],
                gpu=gpu,
            ),
            mounts=example_mounts,
            exclude=ExcludeConfig(
                files=[Path(".env")],
                folders=[Path(".git")],
            ),
            env={
                "TERM": "xterm-256color",
                "PYTHONUNBUFFERED": "1",
            },
            env_files=[Path("~/agents/env/openai.env")],
        )
    return SandboxConfig(
        **_common_skeleton(
            domains=domains,
            project=project,
            extra_packages=[],
            gpu=gpu,
        ),
        mounts=None,
        exclude=None,
        env=None,
        env_files=None,
    )


def starter_template(*, extended: bool = False, gpu: bool = False, project: str) -> str:
    return _HEADER + starter_config(extended=extended, gpu=gpu, project=project).to_yaml()