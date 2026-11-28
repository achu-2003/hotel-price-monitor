#!/bin/sh
# Cloudflare WARP as a SOCKS5 proxy on :40000, for the one site that refuses
# this server's address (Goibibo). See docker/Dockerfile.warp for why.
#
# The WARP account is registered once and kept in /data/warp (a volume), so a
# restart or redeploy is the same device, not a new one every time. Nothing
# about it is a secret anyone has to paste: wgcf asks Cloudflare for a free
# account on first start.
set -eu

ACCOUNT=/data/warp/wgcf-account.toml
PROFILE=/tmp/wgcf-profile.conf
CONFIG=/tmp/wireproxy.conf
# 2408 is WARP's usual port, and on 28 Nov 2026 its handshakes kept dropping
# from a home line while 4500 on the same address held all afternoon. Override
# with WARP_ENDPOINT if Cloudflare ever moves it.
ENDPOINT="${WARP_ENDPOINT:-162.159.192.1:4500}"

mkdir -p /data/warp
cd /data/warp

if [ ! -s "$ACCOUNT" ]; then
    echo "warp: registering a free WARP account"
    # Cloudflare's registration API is occasionally slow; try a few times
    # rather than restart-looping the container.
    n=0
    until wgcf register --accept-tos --config "$ACCOUNT"; do
        n=$((n + 1))
        [ "$n" -ge 5 ] && { echo "warp: registration failed 5 times"; exit 1; }
        sleep 30
    done
fi

wgcf generate --config "$ACCOUNT" --profile "$PROFILE"

PRIVATE_KEY=$(sed -n 's/^PrivateKey = //p' "$PROFILE")
PUBLIC_KEY=$(sed -n 's/^PublicKey = //p' "$PROFILE")
# The profile's Address line is "v4/32, v6/128". Only IPv4 is routed: the
# tunnel was proven that way, and an IPv6 address the host may not carry is
# one more thing to go wrong.
ADDRESS=$(sed -n 's/^Address = \([^,]*\).*/\1/p' "$PROFILE")

cat > "$CONFIG" <<EOF
[Interface]
PrivateKey = $PRIVATE_KEY
Address = $ADDRESS
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = $PUBLIC_KEY
AllowedIPs = 0.0.0.0/0
Endpoint = $ENDPOINT
PersistentKeepalive = 25

[Socks5]
BindAddress = 0.0.0.0:40000
EOF

echo "warp: starting SOCKS5 proxy on :40000 via $ENDPOINT"
exec wireproxy --silent --info 127.0.0.1:9080 --config "$CONFIG"
