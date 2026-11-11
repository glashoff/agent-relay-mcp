"""Container entrypoint, run as root: firewall, then drop to the service user.

Follows base-devcontainer's init-firewall.py: internet allowed, everything
local or private rejected, IPv4 and IPv6; if any rule fails to apply, the
container does not start (fail closed). Then it execs the service through
setpriv as uid/gid VOICE_UID/VOICE_GID (1000, like the host user, so the
mounted sockets work) with no capabilities left (bounding set cleared,
no-new-privs).

Extra addresses to reject (the host's own addresses, which may be public) come
from VOICE_BLOCK_EXTRA, comma-separated, set by the host launcher.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys

PRIVATE_V4 = [
    "0.0.0.0/8",          # "this network"
    "10.0.0.0/8",
    "100.64.0.0/10",      # carrier-grade NAT, Tailscale
    "127.0.0.0/8",        # loopback outside lo
    "169.254.0.0/16",     # link-local
    "172.16.0.0/12",      # includes Docker's bridge 172.17.0.0/16 and the host on it
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",        # multicast
    "240.0.0.0/4",        # reserved, broadcast
]
PRIVATE_V6 = [
    "::/128",
    "::1/128",            # loopback outside lo
    "::ffff:0:0/96",      # IPv4-mapped: would bypass the IPv4 rules
    "64:ff9b:1::/48",     # local-use NAT64
    "100::/64",           # discard
    "2001:db8::/32",      # documentation
    "fc00::/7",           # unique local
    "fe80::/10",          # link-local
    "fec0::/10",          # old site-local
    "ff00::/8",           # multicast
]


def rule(command: str, *arguments: str) -> None:
    """Applies one rule; a failure aborts the start."""
    try:
        subprocess.run([command, *arguments], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        sys.exit(f"firewall: {command} {' '.join(arguments)} failed: {error.stderr.strip()}")
    except OSError as error:
        sys.exit(f"firewall: {command} failed: {error}")


def _default_gateways() -> list[str]:
    """IPv4 default gateways from /proc/net/route (the Docker bridge's host side)."""
    found = []
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    gw = int(fields[2], 16).to_bytes(4, "little")
                    found.append(str(ipaddress.IPv4Address(gw)))
    except (OSError, StopIteration, ValueError):
        pass
    return found


def _ipv6_active() -> bool:
    try:
        with open("/proc/net/if_inet6") as f:
            return any(line.strip() for line in f)
    except OSError:
        return False


def extra_targets() -> tuple[list[str], list[str]]:
    v4, v6 = [], []
    for item in os.environ.get("VOICE_BLOCK_EXTRA", "").split(",") + _default_gateways():
        item = item.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            sys.exit(f"firewall: VOICE_BLOCK_EXTRA has an invalid address {item!r}")
        (v4 if net.version == 4 else v6).append(str(net))
    return v4, v6


def apply() -> None:
    extra_v4, extra_v6 = extra_targets()
    rule("iptables", "-F", "OUTPUT")
    rule("iptables", "-F", "INPUT")
    rule("iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    for network in PRIVATE_V4 + extra_v4:
        rule("iptables", "-A", "OUTPUT", "-d", network,
             "-j", "REJECT", "--reject-with", "icmp-net-prohibited")
    # Nothing comes in except replies; the service listens on nothing reachable.
    rule("iptables", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT")
    rule("iptables", "-A", "INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
         "-j", "ACCEPT")
    rule("iptables", "-P", "INPUT", "DROP")

    ip6 = subprocess.run(["ip6tables", "-L", "-n"], capture_output=True).returncode == 0
    if ip6:
        rule("ip6tables", "-F", "OUTPUT")
        rule("ip6tables", "-F", "INPUT")
        rule("ip6tables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
        for network in PRIVATE_V6 + extra_v6:
            rule("ip6tables", "-A", "OUTPUT", "-d", network,
                 "-j", "REJECT", "--reject-with", "adm-prohibited")
        rule("ip6tables", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT")
        rule("ip6tables", "-A", "INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
             "-j", "ACCEPT")
        rule("ip6tables", "-P", "INPUT", "DROP")
    elif _ipv6_active():
        sys.exit("firewall: IPv6 is active but ip6tables does not work; not starting")
    print("firewall: local and private networks blocked"
          + ("" if ip6 else " (IPv6 not available in this container)"), flush=True)


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("firewall: must start as root (the image's entrypoint does that)")
    apply()
    uid = os.environ.get("VOICE_UID", "1000")
    gid = os.environ.get("VOICE_GID", "1000")
    command = sys.argv[1:] or ["python3", "-m", "agent_relay_voice", "serve"]
    os.execvp("setpriv", [
        "setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups",
        "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", "--no-new-privs",
        "--", *command,
    ])


if __name__ == "__main__":
    main()
