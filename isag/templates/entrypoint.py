"""Entrypoint shell script — emitted alongside the Dockerfile."""

from isag.models import SandboxConfig

ENTRYPOINT_SH = r"""#!/usr/bin/env bash
# Entrypoint — strict network sandbox.
#
# For each domain we query EVERY configured upstream resolver AND the
# in-container resolver, then union all returned IPv4s into the allowlist.
# This covers IP-rotation across resolvers (CDN, geo-anycast) and ensures
# the runtime resolver's answer is always allowlisted regardless of which
# upstream Docker's embedded DNS proxy ends up using.

set -Eeuo pipefail

DOMAINS_FILE="/etc/isag/allowed-domains.txt"
: "${RUN_AS_USER:?must be set}"
: "${ISAG_RESOLVERS:?must be set}"
: "${AGENT_VENDOR:?must be set}"

[[ $# -gt 0 ]]            || { echo "entrypoint: no command given" >&2; exit 1; }
[[ -s "$DOMAINS_FILE" ]]  || { echo "entrypoint: missing/empty $DOMAINS_FILE" >&2; exit 1; }

# Bind-mount sources may be root-owned if docker auto-created them on a prior
# run. Chown the bind-mount targets so the runtime user can write into them.
# /home/$RUN_AS_USER itself stays image-default (created by useradd) and is
# intentionally not bind-mounted, so vendors can't see each other's state.
chown "$RUN_AS_USER:$RUN_AS_USER" \
    "/home/$RUN_AS_USER/.$AGENT_VENDOR" \
    "/home/$RUN_AS_USER/.cache"

# sshd reads /etc/isag/authorized_keys directly (AuthorizedKeysFile in the
# sshd drop-in), so the host's id_ed25519.pub bind mount is the live source
# of truth — regenerating the keypair on host immediately takes effect with
# no stale in-container copy. Start sshd before the iptables seal; the
# seal's -i lo ACCEPT keeps it reachable from `docker exec` (used by
# `isag ssh` on the host).
# -D -e &: foreground mode logging to stderr, backgrounded by the shell.
# Plain `sshd -e` daemonizes, and OpenSSH closes 0/1/2 during daemonization
# — so "-e" would log to a closed fd and every "Failed publickey" /
# StrictModes-reject line would vanish, leaving auth failures undiagnosable.
# Stderr is captured to a file (not the container's stderr) to keep the
# agent's terminal clean from sshd's per-connection chatter at LogLevel
# VERBOSE; `docker exec <c> cat /var/log/isag-sshd.log` recovers it when
# debugging an auth failure.
/usr/sbin/sshd -D -e 2>/var/log/isag-sshd.log &

read -r -a UPSTREAMS <<< "$ISAG_RESOLVERS"
[[ ${#UPSTREAMS[@]} -gt 0 ]] || { echo "entrypoint: ISAG_RESOLVERS is empty" >&2; exit 1; }

mapfile -t LOCAL_DNS < <(awk '/^nameserver / {print $2}' /etc/resolv.conf)
[[ ${#LOCAL_DNS[@]} -gt 0 ]] || { echo "entrypoint: no resolvers in /etc/resolv.conf" >&2; exit 1; }

echo "entrypoint: querying upstreams + local resolver: ${UPSTREAMS[*]} | ${LOCAL_DNS[*]}"
echo "entrypoint: resolving allowlist..."

ipset create agent_allowed hash:ip family inet -exist
ipset flush  agent_allowed

resolve_one() {
    # Query a single resolver, output one IP per line. Empty on failure.
    local resolver="$1" domain="$2"
    dig +short +time=2 +tries=1 @"$resolver" A "$domain" 2>/dev/null \
        | awk '/^[0-9.]+$/' | sort -u
}

resolve_local() {
    # Use the in-container resolver path (= what the agent will use at
    # runtime). Same IPs Docker's embedded DNS will hand out.
    local domain="$1"
    getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true
}

while IFS= read -r domain; do
    [[ -n "$domain" ]] || continue

    declare -A ip_sources=()  # ip -> space-separated list of resolvers
    declare -a ordered_ips=()

    add_ips() {
        local source="$1" ips="$2" ip
        [[ -z "$ips" ]] && return
        while read -r ip; do
            if [[ -z "${ip_sources[$ip]+x}" ]]; then
                ordered_ips+=("$ip")
                ip_sources[$ip]="$source"
            else
                ip_sources[$ip]+=",$source"
            fi
        done <<< "$ips"
    }

    # Every upstream + the local resolver. Union, not first-success.
    for r in "${UPSTREAMS[@]}"; do
        add_ips "$r" "$(resolve_one "$r" "$domain")"
    done
    add_ips "local" "$(resolve_local "$domain")"

    if [[ ${#ordered_ips[@]} -eq 0 ]]; then
        echo "entrypoint: failed to resolve $domain via any resolver" >&2
        exit 1
    fi

    printf '  %s:\n' "$domain"
    for ip in "${ordered_ips[@]}"; do
        ipset add agent_allowed "$ip" -exist
        printf '    %-18s (via %s)\n' "$ip" "${ip_sources[$ip]}"
    done

    unset ip_sources ordered_ips
done < "$DOMAINS_FILE"

echo "entrypoint: sealing iptables..."

iptables -F; iptables -X
iptables -P INPUT   DROP
iptables -P FORWARD DROP
iptables -P OUTPUT  DROP

iptables -A INPUT   -i lo -j ACCEPT
iptables -A OUTPUT  -o lo -j ACCEPT
iptables -A INPUT   -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# {{INTRA_NETWORK_RULES}}
# Runtime DNS. Agents query the embedded proxy at 127.0.0.11; the proxy
# forwards to the upstream resolvers from within this same network
# namespace, so both destinations need a hole punched.
for r in "${LOCAL_DNS[@]}" "${UPSTREAMS[@]}"; do
    iptables -A OUTPUT -p udp -d "$r" --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp -d "$r" --dport 53 -j ACCEPT
done

iptables -A OUTPUT -m set --match-set agent_allowed dst -j ACCEPT

if [[ -d /proc/sys/net/ipv6 ]] && command -v ip6tables >/dev/null; then
    ip6tables -F; ip6tables -X
    ip6tables -P INPUT   DROP
    ip6tables -P FORWARD DROP
    ip6tables -P OUTPUT  DROP
    ip6tables -A INPUT  -i lo -j ACCEPT
    ip6tables -A OUTPUT -o lo -j ACCEPT
fi

echo "entrypoint: dropping to $RUN_AS_USER"
exec gosu "$RUN_AS_USER" "$@"
"""


ENTRYPOINT_SH_UNRESTRICTED = r"""#!/usr/bin/env bash
# isag entrypoint — limit_network is null, no iptables sandbox applied.

set -Eeuo pipefail

: "${RUN_AS_USER:?must be set}"
: "${AGENT_VENDOR:?must be set}"

[[ $# -gt 0 ]] || { echo "entrypoint: no command given" >&2; exit 1; }

# Bind-mount sources may be root-owned if docker auto-created them on a prior
# run. Chown the bind-mount targets so the runtime user can write into them.
chown "$RUN_AS_USER:$RUN_AS_USER" \
    "/home/$RUN_AS_USER/.$AGENT_VENDOR" \
    "/home/$RUN_AS_USER/.cache"

# sshd reads /etc/isag/authorized_keys directly (AuthorizedKeysFile in the
# sshd drop-in) — the bind mount is the live source. Launch on 127.0.0.1:22
# for host-side `isag ssh`. -D -e &: foreground + log to stderr, backgrounded
# by the shell. Plain `sshd -e` daemonizes and closes 0/1/2, swallowing auth
# failure logs. Stderr is captured to a file so the agent's terminal stays
# clean; `docker exec <c> cat /var/log/isag-sshd.log` recovers it on demand.
/usr/sbin/sshd -D -e 2>/var/log/isag-sshd.log &

echo "entrypoint: limit_network is null — outbound traffic is unrestricted"
echo "entrypoint: dropping to $RUN_AS_USER"
exec gosu "$RUN_AS_USER" "$@"
"""


def render_entrypoint(config: SandboxConfig) -> str:
    if config.limit_network is None:
        return ENTRYPOINT_SH_UNRESTRICTED
    if config.container.external_networks:
        # Allow the agent to reach sibling containers on shared external
        # networks. Replies come back via the ESTABLISHED,RELATED rule above.
        intra = (
            "iptables -A OUTPUT -d 10.0.0.0/8     -j ACCEPT\n"
            "iptables -A OUTPUT -d 172.16.0.0/12  -j ACCEPT\n"
            "iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT\n"
        )
        return ENTRYPOINT_SH.replace("# {{INTRA_NETWORK_RULES}}\n", intra)
    return ENTRYPOINT_SH.replace("# {{INTRA_NETWORK_RULES}}\n", "")