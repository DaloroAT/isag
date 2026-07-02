"""Dockerfile and .dockerignore templates — static assets.

ARG declarations live just above the layer that first references them.
No ARG defaults: build args must arrive explicitly.
"""

from isag.models import SandboxConfig


_DOCKERFILE = """\
# Auto-generated. Do not edit by hand.
ARG BASE_IMAGE
FROM ${{BASE_IMAGE}}
USER root
WORKDIR /

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
# in at runtime. `AllowTcpForwarding yes` permits both -L (forward container
# services to host) and -R (forward host services into the container's
# loopback) so the host-side developer can expose things like an adb server
# or DB proxy without disabling the network firewall. `GatewayPorts no`
# keeps -R listeners bound to 127.0.0.1, so nothing leaks onto the
# container's external interfaces.
RUN printf '%s\\n' \\
        'ListenAddress 127.0.0.1' \\
        'PasswordAuthentication no' \\
        'PermitRootLogin no' \\
        'PubkeyAuthentication yes' \\
        'StrictModes no' \\
        'UsePAM no' \\
        'PrintMotd no' \\
        'HostKey /etc/ssh/host_keys/ssh_host_ed25519_key' \\
        'AuthorizedKeysFile /etc/isag/authorized_keys' \\
        'AllowTcpForwarding yes' \\
        'GatewayPorts no' \\
        'LogLevel VERBOSE' \\
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

# Runtime user, created at the host user's UID/GID (passed as build args so
# bind-mounted files stay writable and sshd's StrictModes accepts the
# host-owned authorized_keys). --non-unique tolerates a UID/GID that already
# exists in the base image; --no-log-init stops useradd writing a sparse
# /var/log/lastlog+/var/log/faillog record at offset UID*recsize, which for a
# large UID would balloon the image layer by gigabytes.
# -p '*' sets the shadow password to a literal '*' — still unloginable via
# password (not a valid hash), but not "locked". useradd's default is '!',
# which sshd's allowed_user() rejects as "account is locked" before it ever
# checks the pubkey, regardless of UsePAM/PubkeyAuthentication settings.
ARG USER_NAME
ARG USER_UID
ARG USER_GID
ENV RUN_AS_USER=${{USER_NAME}}
ENV HOME=/home/${{USER_NAME}}
RUN set -eu; \\
    if id -u "${{USER_NAME}}" >/dev/null 2>&1; then \\
        existing_uid="$(id -u "${{USER_NAME}}")"; \\
        if [ "$existing_uid" != "${{USER_UID}}" ]; then \\
            echo "user ${{USER_NAME}} already exists with UID $existing_uid, expected ${{USER_UID}}; set container.uid to match or pick another container.user" >&2; \\
            exit 1; \\
        fi; \\
        usermod -p '*' -d "/home/${{USER_NAME}}" -s /bin/bash "${{USER_NAME}}"; \\
        mkdir -p "/home/${{USER_NAME}}"; \\
    else \\
        if ! getent group "${{USER_GID}}" >/dev/null; then \\
            group_name="${{USER_NAME}}"; \\
            getent group "${{USER_NAME}}" >/dev/null && group_name="grp_${{USER_GID}}"; \\
            groupadd --gid "${{USER_GID}}" "$group_name"; \\
        fi; \\
        useradd --no-log-init --create-home --home-dir "/home/${{USER_NAME}}" --uid "${{USER_UID}}" --non-unique --gid "${{USER_GID}}" --shell /bin/bash -p '*' "${{USER_NAME}}"; \\
    fi; \\
    chown "${{USER_NAME}}:" "/home/${{USER_NAME}}"

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
