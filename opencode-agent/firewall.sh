#!/bin/sh
# Egress firewall for the agent container. Runs as root once at startup,
# before the agent drops to the unprivileged "node" user with no capabilities,
# so nothing the agent runs can change these rules afterwards.
#
# Policy: the open internet is allowed. Every private, link-local and
# loopback-style range is rejected, which covers the Windows host and WSL
# (including its DNS relay), the Docker gateways, your home LAN,
# and the other containers. The only private destinations allowed are the
# ones listed in ALLOW_INTERNAL (host:port pairs), i.e. the llm-proxy, on
# that port across the internal network it shares with the agent.
set -eu

ALLOW_INTERNAL="${ALLOW_INTERNAL:-llm-proxy:8080}"
BLOCKED_V4="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4"

# Pick an iptables backend that works on this kernel.
IPT=""
for candidate in iptables iptables-legacy; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -w -L OUTPUT -n >/dev/null 2>&1; then
    IPT="$candidate"
    break
  fi
done
if [ -z "$IPT" ]; then
  echo "firewall: no working iptables backend; refusing to start without a firewall" >&2
  exit 1
fi
IP6T="$(echo "$IPT" | sed 's/^iptables/ip6tables/')"

# in_subnet 172.20.0.5 172.20.0.3/16 -> true when the address is inside the subnet.
in_subnet() {
  echo "$1 $2" | awk '{
    split($1, a, "."); split($2, c, "/"); split(c[1], b, ".")
    size = 2 ^ (32 - c[2])
    ip = ((a[1] * 256 + a[2]) * 256 + a[3]) * 256 + a[4]
    net = ((b[1] * 256 + b[2]) * 256 + b[3]) * 256 + b[4]
    exit !(int(ip / size) == int(net / size))
  }'
}

$IPT -w -F OUTPUT
$IPT -w -P OUTPUT ACCEPT
$IPT -w -A OUTPUT -o lo -j ACCEPT
# Replies on connections someone else opened to us (the bridge calling :4096).
$IPT -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

for entry in $ALLOW_INTERNAL; do
  host="${entry%:*}"
  port="${entry##*:}"
  ips="$(getent ahostsv4 "$host" | awk '{print $1}' | sort -u)"
  if [ -z "$ips" ]; then
    echo "firewall: cannot resolve $host; refusing to start" >&2
    exit 1
  fi
  # Allow the whole network the host sits on rather than its current address,
  # so the host stays reachable when its container is recreated with a new IP.
  # The host must be inside a subnet attached to this container: anything
  # reached through a gateway or tunnel would open the port more widely.
  for ip in $ips; do
    dev="$(ip -4 route get "$ip" | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')"
    net=""
    for cidr in $(ip -4 -o addr show dev "$dev" | awk '{print $4}'); do
      if in_subnet "$ip" "$cidr"; then
        net="$cidr"
        break
      fi
    done
    if [ -z "$net" ]; then
      echo "firewall: $host ($ip) is not on a network attached to this container; refusing to start" >&2
      exit 1
    fi
    $IPT -w -A OUTPUT -d "$net" -p tcp --dport "$port" -j ACCEPT
    echo "firewall: allow $host's network ($net) tcp/$port"
  done
done

for net in $BLOCKED_V4; do
  $IPT -w -A OUTPUT -d "$net" -p tcp -j REJECT --reject-with tcp-reset
  $IPT -w -A OUTPUT -d "$net" -j REJECT --reject-with icmp-admin-prohibited
done

# IPv6: compose disables it, but if it is present, allow loopback only.
if command -v "$IP6T" >/dev/null 2>&1 && "$IP6T" -w -L OUTPUT -n >/dev/null 2>&1; then
  $IP6T -w -F OUTPUT
  $IP6T -w -A OUTPUT -o lo -j ACCEPT
  $IP6T -w -P OUTPUT DROP
fi

echo "firewall: blocked private ranges via $IPT; open internet allowed"
