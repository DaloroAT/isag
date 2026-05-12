"""Dockerfile and .dockerignore templates — static assets.

ARG declarations live just above the layer that first references them.
No ARG defaults: build args must arrive explicitly.
"""

from isag.models import SandboxConfig


_DOCKERFILE = """\
# Auto-generated. Do not edit by hand.
ARG BASE_IMAGE
FROM ${{BASE_IMAGE}}

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Base tooling.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl gnupg software-properties-common \\
        dnsutils \\
        git gosu iproute2 ipset iptables jq openssh-server ripgrep socat wget \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /var/run/sshd /etc/ssh/host_keys

# sshd drop-in: loopback only, key-only auth, ed25519 host key bind-mounted
# in at runtime. `AllowTcpForwarding local` permits -L (forward container
# services to host) but refuses -R, keeping the "container can't reach
# back to host" invariant intact.
RUN printf '%s\\n' \\
        'ListenAddress 127.0.0.1' \\
        'PasswordAuthentication no' \\
        'PermitRootLogin no' \\
        'PubkeyAuthentication yes' \\
        'UsePAM no' \\
        'PrintMotd no' \\
        'HostKey /etc/ssh/host_keys/ssh_host_ed25519_key' \\
        'AllowTcpForwarding local' \\
        'GatewayPorts no' \\
        > /etc/ssh/sshd_config.d/10-isag.conf

# Node.js.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

# Python via deadsnakes.
ARG PYTHON_VERSION
RUN add-apt-repository -y ppa:deadsnakes/ppa \\
    && apt-get update && apt-get install -y --no-install-recommends \\
        python${{PYTHON_VERSION}} \\
        python${{PYTHON_VERSION}}-venv \\
        python${{PYTHON_VERSION}}-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && ln -sf /usr/bin/python${{PYTHON_VERSION}} /usr/local/bin/python3 \\
    && ln -sf /usr/bin/python${{PYTHON_VERSION}} /usr/local/bin/python

# pip — Ubuntu disables ensurepip for distro Pythons.
RUN curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \\
    && python /tmp/get-pip.py \\
    && rm /tmp/get-pip.py
    
{extra_packages_block}

# Agent CLI. ADD line cache-busts: 404 on bad versions; JSON changes when 'latest' moves.
ARG AGENT_PACKAGE
ARG AGENT_CLI_VERSION
ADD https://registry.npmjs.org/${{AGENT_PACKAGE}}/${{AGENT_CLI_VERSION}} /tmp/agent-version.json
RUN npm install -g ${{AGENT_PACKAGE}}@${{AGENT_CLI_VERSION}} \\
    && rm /tmp/agent-version.json

# Runtime user. Free UID 1000 first (ubuntu base images claim it).
ARG USER_NAME
ENV RUN_AS_USER=${{USER_NAME}}
RUN userdel -r ubuntu 2>/dev/null || true; \\
    groupdel ubuntu 2>/dev/null || true; \\
    useradd --create-home --uid 1000 --shell /bin/bash ${{USER_NAME}}

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
"""

# Excludes the entire build context except files referenced by Dockerfile.
DOCKERIGNORE = """\
*
!Dockerfile
!entrypoint.sh
"""


def _extra_packages_block(packages: list[str]) -> str:
    """Render the apt-install block for user-declared extra packages.

    Placed before the agent CLI install: the CLI install layer is the most
    frequently-invalidated step (version bumps, dist-tag moves), so keeping
    user packages above it means CLI churn doesn't redo `apt install`.
    Returns empty string when packages is empty so no extra layer is added.
    """
    if not packages:
        return ""
    indent = "        "
    pkg_lines = " \\\n".join(indent + p for p in packages)
    return (
        "\n# Extra packages from container.extra_packages.\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        f"{pkg_lines} \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
    )


def render_dockerfile(config: SandboxConfig) -> str:
    return _DOCKERFILE.format(
        extra_packages_block=_extra_packages_block(config.container.extra_packages)
    )


def render_dockerignore() -> str:
    return DOCKERIGNORE