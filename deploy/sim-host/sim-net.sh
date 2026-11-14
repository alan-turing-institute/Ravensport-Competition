#!/bin/sh
# shellcheck shell=sh
# Load the actor packet filter, then plumb the sim-host interface addresses and
# source-based policy routing. Run at start-up by the sim-net OpenRC service,
# before the sim and HAProxy services (sim-network.md §2). Idempotent: every
# step overwrites prior state, so a service restart or a manual re-run
# reconfigures cleanly rather than aborting.
#
# Each interface gets its address and a policy-routing table holding the
# on-link subnet route and a default via the zone firewall gateway, plus an
# `ip rule` selecting that table by source address. Destination-only routing
# cannot disambiguate the five interfaces sharing 10.40.0.0/24 — it would pick
# one route for the whole subnet — so source policy routing makes every flow
# leave through its own interface with its own MAC. Applied uniformly to every
# interface (egress and appliance alike), so adding an entity is one row below
# and cannot silently misroute through a sibling. No global default route is
# needed: every flow the sim originates is source-bound to a managed address.
set -eu

# Single per-interface list (sim-network.md §2). All sim-host subnets are /24.
#   name           address     prefix  zone-gateway  table
NICS="
ot-atg         10.40.0.51 24 10.40.0.2 51
ot-disp        10.40.0.52 24 10.40.0.2 52
ot-vessel-rfid 10.40.0.53 24 10.40.0.2 53
ot-op-rfid     10.40.0.54 24 10.40.0.2 54
ot-operator    10.40.0.55 24 10.40.0.2 55
ext-supplier   10.10.0.10 24 10.10.0.2 10
it-recon-user  10.30.0.11 24 10.30.0.2 11
it-sysadmin    10.30.0.12 24 10.30.0.2 12
"

# sim-host is a multi-homed endpoint on vnet-ext, vnet-ot, and vnet-it, never a
# router between them. Disable IPv4 forwarding so the kernel cannot bridge the
# three zones through their directly connected routes; the forward-chain drop in
# egress-guard.nft enforces the same property at the packet filter. Set before
# any interface goes live, so no window exists in which forwarding is enabled.
echo 0 > /proc/sys/net/ipv4/ip_forward

# Reverse-path filtering consults the main table, but every cross-zone return
# route lives in a per-source policy table below (there is no route in main to,
# for example, the ATG relay's zone). Strict or loose RPF therefore drops
# legitimate inbound flows whose source is off-link — the relay reaching the ATG
# console was dropped this way. Disable it: ip_forward=0 and egress-guard.nft
# already confine this endpoint, so RPF adds no protection here. rp_filter is
# also set per interface below, but the kernel takes max(all, iface), so the
# `all` copy must be cleared too or it wins.
echo 0 > /proc/sys/net/ipv4/conf/all/rp_filter

# Drop inbound on the pure-egress interfaces (the appliance interfaces serve),
# and drop all forwarded traffic, before any interface carries traffic, so no
# interface is ever live without its filter. The filter matches by iifname
# (interface name), resolved per packet, so it loads cleanly before the
# interfaces exist.
nft -f /etc/nftables/egress-guard.nft

echo "$NICS" | while read -r name addr prefix gw table; do
    [ -n "$name" ] || continue
    subnet="${addr%.*}.0/$prefix"

    ip address replace "$addr/$prefix" dev "$name"

    # ARP and reverse-path hardening, set before the link carries traffic. Five
    # addresses share 10.40.0.0/24, so with the kernel defaults sim-host would
    # answer ARP for any local address out of any interface and source its
    # announcements arbitrarily; a peer could then cache a service address
    # against a sibling interface's MAC, and — because macvlan filters received
    # frames by MAC — that traffic would land on the wrong sub-interface and be
    # dropped. arp_ignore=1 answers only for the receiving interface's own
    # address; arp_announce=2 always sources the announcement from it. rp_filter=0
    # matches the `all` setting above (return routes are per-source, not in main).
    echo 1 > "/proc/sys/net/ipv4/conf/$name/arp_ignore"
    echo 2 > "/proc/sys/net/ipv4/conf/$name/arp_announce"
    echo 0 > "/proc/sys/net/ipv4/conf/$name/rp_filter"

    ip link set "$name" up

    # Source-based policy table: on-link subnet + default via the zone gateway.
    ip route replace "$subnet" dev "$name" src "$addr" table "$table"
    ip route replace default via "$gw" dev "$name" table "$table"
    # `ip rule` has no replace verb; drop any prior copy, then add exactly one.
    ip rule del from "$addr" lookup "$table" 2>/dev/null || true
    ip rule add from "$addr" lookup "$table"
done
