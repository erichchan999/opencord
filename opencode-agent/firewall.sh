#!/bin/sh
# Egress firewall for the agent container. Runs as root once at startup,
# before the agent drops to the unprivileged "node" user with no capabilities,
# so nothing the agent runs can change these rules afterwards.
#
# Policy: the open internet is allowed. Every private, link-local and
# loopback-style range is rejected, which covers the Windows host and WSL
# (including its DNS relay), the Docker gateways, your home LAN,
# and the other containers. The only private destinations allowed are the
# ones listed in ALLOW_INTERNAL (host:port pairs), i.e. the llm-proxy.
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
  for ip in $ips; do
    $IPT -w -A OUTPUT -d "$ip" -p tcp --dport "$port" -j ACCEPT
    echo "firewall: allow $host ($ip) tcp/$port"
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
