#!/usr/bin/env bash
# Public-only egress on the disposable Actions runner. This closes the gap
# between validating a hostname and a later DNS resolution by HTTP/Chromium.
set -euo pipefail

readonly CHAIN=PORTFOLIO_IT_PUBLIC
readonly -a IPV4_DENY=(
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16
  168.63.129.16/32 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.88.99.0/24 192.168.0.0/16
  198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4
)
readonly -a IPV6_DENY=(2001::/23 2001:db8::/32 2002::/16 3fff::/20)

firewall() { local command="$1"; shift; sudo -n "$command" -w 5 "$@"; }

stop_guard() {
  # Never flush OUTPUT or change its policy; remove only this task's chain.
  local command
  for command in iptables ip6tables; do
    while firewall "$command" -C OUTPUT -j "$CHAIN" 2>/dev/null; do
      firewall "$command" -D OUTPUT -j "$CHAIN"
    done
    if firewall "$command" -S "$CHAIN" >/dev/null 2>&1; then
      firewall "$command" -F "$CHAIN"
      firewall "$command" -X "$CHAIN"
    fi
  done
}

check_guard() {
  local network
  firewall iptables -C OUTPUT -j "$CHAIN"
  firewall ip6tables -C OUTPUT -j "$CHAIN"
  for network in "${IPV4_DENY[@]}"; do
    firewall iptables -C "$CHAIN" -d "$network" -j REJECT
  done
  for network in "${IPV6_DENY[@]}"; do
    firewall ip6tables -C "$CHAIN" -d "$network" -j REJECT
  done
  firewall ip6tables -C "$CHAIN" -d 2000::/3 -j RETURN
  firewall ip6tables -C "$CHAIN" -j REJECT
}

start_guard() {
  local command network family address protocol resolvers
  for command in iptables ip6tables; do
    if firewall "$command" -S "$CHAIN" >/dev/null 2>&1; then
      echo 'IT public network guard already exists; refusing to replace it.' >&2
      return 1
    fi
  done
  trap stop_guard EXIT
  firewall iptables -N "$CHAIN"
  firewall ip6tables -N "$CHAIN"
  # A runner can use a private or loopback DNS stub. Allow only its DNS port,
  # never other services on that address. No blanket private-network exception.
  resolvers="$(python - <<'PY'
import ipaddress
from pathlib import Path
seen = set()
# systemd-resolved can expose only a loopback stub in /etc/resolv.conf; its
# configured upstream resolvers still need DNS-only access to answer queries.
for name in ('/etc/resolv.conf', '/run/systemd/resolve/resolv.conf'):
    path = Path(name)
    if not path.is_file():
        continue
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == 'nameserver':
            address = ipaddress.ip_address(fields[1])
            if '%' in str(address):
                raise ValueError('Scoped DNS resolver requires explicit interface handling')
            if address not in seen:
                print(address.version, str(address))
                seen.add(address)
if not seen:
    raise ValueError('No configured DNS resolver found')
PY
  )"
  while read -r family address; do
    test -n "$address" || continue
    if test "$family" = 4; then command=iptables; else command=ip6tables; fi
    for protocol in udp tcp; do
      firewall "$command" -A "$CHAIN" -d "$address" -p "$protocol" --dport 53 -j RETURN
      # A loopback DNS stub sends its answer through OUTPUT too. Permit only
      # replies belonging to an existing DNS query, not arbitrary local ports.
      firewall "$command" -A "$CHAIN" -s "$address" -p "$protocol" --sport 53 -m conntrack --ctstate ESTABLISHED -j RETURN
    done
  done <<< "$resolvers"
  for network in "${IPV4_DENY[@]}"; do
    firewall iptables -A "$CHAIN" -d "$network" -j REJECT
  done
  for network in "${IPV6_DENY[@]}"; do
    firewall ip6tables -A "$CHAIN" -d "$network" -j REJECT
  done
  # Non-global IPv6, including mapped IPv4, NAT64, ULA and link-local, has no
  # egress path. Restrict global unicast further by the special-use ranges above.
  firewall ip6tables -A "$CHAIN" -d 2000::/3 -j RETURN
  firewall ip6tables -A "$CHAIN" -j REJECT
  firewall iptables -I OUTPUT 1 -j "$CHAIN"
  firewall ip6tables -I OUTPUT 1 -j "$CHAIN"
  check_guard
  trap - EXIT
  echo 'IT public-only network guard active.'
}

case "${1:-}" in
  start) start_guard ;;
  check) check_guard ;;
  stop) stop_guard ;;
  *) echo 'Usage: it_public_network.sh start|check|stop' >&2; exit 2 ;;
esac
