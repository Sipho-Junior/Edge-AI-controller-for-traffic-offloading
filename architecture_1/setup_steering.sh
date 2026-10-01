#!/bin/bash
# =====================================================================
# setup_steering.sh -- build the dual-path steering datapath on eno1.
#
# Why this exists
# ---------------
# User-plane traffic crosses eno1 GTP-U-encapsulated (UDP 2152). A tc
# u32 filter reads the OUTER IP header, so the application's DSCP --
# which lives in the INNER header -- is invisible to it. OAI never
# copies it outward (openair3/ocp-gtpu/gtp_itf.cpp sets only SO_SNDBUF),
# so every user packet leaves eno1 with outer ToS 0x00 and lands in the
# terrestrial class no matter what the controller decides.
#
# This script installs the missing step: an iptables mangle chain that
# reaches into the GTP-U payload, identifies the traffic class by its
# INNER UDP destination port, and stamps a distinct codepoint on the
# OUTER header. tc then steers on that outer codepoint as it always did.
#
# Class is keyed on inner port rather than inner DSCP because Voice-EF
# and Haptic-EF share DSCP EF (0x2e) by design, yet carry different 5QI
# (1 vs 87) and different PDBs (100 ms vs 5 ms). In the labelled data
# they disagree in 8 of 42 condition pairs. DSCP alone cannot express
# that split; the inner port can.
#
# Run as root:  sudo ./setup_steering.sh
# =====================================================================
set -u

IFACE="${IFACE:-eno1}"
CORE_IP="${CORE_IP:-10.45.0.1}"
CHAIN="NTNMARK"

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root: sudo $0" >&2
  exit 1
fi

# Inner UDP dest port -> outer DSCP. All four distinct, all standard
# codepoints, so every class is independently steerable.
#   port : dscp : tos(=dscp<<2) : label
MAP=(
  "5201:0x2e:0xb8:Voice-EF"
  "5202:0x22:0x88:Video-AF41"
  "5203:0x28:0xa0:Haptic-EF"
  "5204:0x08:0x20:Background-BE"
)

# ------------------------------------------------------------ tc ----
# 1:10 terrestrial user plane   (rate cap applied per condition)
# 1:20 NTN user plane           (netem applied per condition)
# 1:99 unshaped control/mgmt    (SSH, NGAP, RFSim -- NOT part of the
#      experiment, so it must never inherit a rate cap or 55 ms of
#      netem; htb default points here)
build_tc() {
  tc qdisc del dev "$IFACE" root 2>/dev/null

  tc qdisc add dev "$IFACE" root handle 1: htb default 99
  tc class add dev "$IFACE" parent 1:  classid 1:10 htb rate 1Gbit ceil 1Gbit
  tc class add dev "$IFACE" parent 1:  classid 1:20 htb rate 1Gbit ceil 1Gbit
  tc class add dev "$IFACE" parent 1:  classid 1:99 htb rate 1Gbit ceil 1Gbit
  tc qdisc add dev "$IFACE" parent 1:10 handle 10: netem delay 0ms limit 1000
  tc qdisc add dev "$IFACE" parent 1:20 handle 20: netem delay 0ms limit 1000

  # Steering filters. The controller rewrites the flowid of these at
  # runtime; they start on terrestrial.
  local prio=5
  for entry in "${MAP[@]}"; do
    IFS=':' read -r port dscp tos label <<< "$entry"
    tc filter add dev "$IFACE" parent 1: protocol ip prio "$prio" u32 \
       match ip tos "$tos" 0xfc flowid 1:10
    prio=$((prio + 1))
  done
  echo "[tc] htb rebuilt: 1:10 terrestrial, 1:20 ntn, 1:99 unshaped (default)"
}

# ------------------------------------------------- GTP offset probe ----
# Inner IP begins at (UDP header 8) + (GTP-U header len) bytes past the
# start of the outer transport header. The GTP-U header length is not
# fixed: OAI adds a PDU Session Container extension carrying the QFI on
# the N3 uplink, and may add a second extension. Rather than hardcode a
# guess, probe for the offset that actually puts the inner destination
# address where it belongs.
#   8+8  = 16  base header, no extension
#   8+12 = 20  sequence number present, no extension
#   8+16 = 24  one extension  (expected for N3 uplink: PDU Session Container)
#   8+20 = 28  two extensions
CANDIDATES=(24 16 20 28)

ip_to_hex() { printf '0x%02x%02x%02x%02x' ${1//./ }; }
CORE_HEX=$(ip_to_hex "$CORE_IP")

probe_offset() {
  local found=""
  for off in "${CANDIDATES[@]}"; do
    iptables -t mangle -F "$CHAIN" 2>/dev/null
    # Count packets whose inner destination address == the core IP when
    # the inner header is assumed to start at $off.
    iptables -t mangle -A "$CHAIN" -p udp --dport 2152 \
      -m u32 --u32 "0>>22&0x3C@$((off+16))=$CORE_HEX" \
      -j RETURN 2>/dev/null || continue

    # Drive a short burst of user-plane traffic through the tunnel.
    # ICMP is used rather than iperf3 because the probe only reads the
    # inner destination address, which sits in the inner IP header and
    # is therefore protocol-independent -- and ping needs no listening
    # server on the far side.
    ping -I oaitun_ue1 -c 10 -i 0.2 -W 1 "$CORE_IP" >/dev/null 2>&1

    local n
    n=$(iptables -t mangle -L "$CHAIN" -vxn 2>/dev/null | awk '/RETURN/{print $1; exit}')
    n=${n:-0}
    echo "  offset $off -> $n packets matched" >&2
    if [ "$n" -gt 0 ]; then found="$off"; break; fi
  done
  iptables -t mangle -F "$CHAIN" 2>/dev/null
  echo "$found"
}

# ------------------------------------------------------- iptables ----
build_iptables() {
  local off="$1"
  iptables -t mangle -F "$CHAIN" 2>/dev/null

  for entry in "${MAP[@]}"; do
    IFS=':' read -r port dscp tos label <<< "$entry"
    # Match: outer is GTP-U, inner protocol is UDP (0x11), inner UDP
    # destination port is this class's port. Then stamp the outer DSCP.
    #   inner proto     : inner+9   -> word at (off+8), byte 1
    #   inner dest port : inner+22  -> word at (off+20), low 16 bits
    # Assumes inner IHL == 5 (no IP options), which holds for the
    # iperf3/ping traffic this testbed generates.
    iptables -t mangle -A "$CHAIN" -p udp --dport 2152 \
      -m u32 --u32 "0>>22&0x3C@$((off+8))>>16&0xFF=0x11 && 0>>22&0x3C@$((off+20))&0xFFFF=$port" \
      -j DSCP --set-dscp "$dscp"
    echo "  $label (inner udp/$port) -> outer DSCP $dscp (tos $tos)"
  done
}

# ----------------------------------------------------------- main ----
echo "=== Building tc hierarchy on $IFACE ==="
build_tc

echo
echo "=== Installing mangle chain $CHAIN ==="
iptables -t mangle -N "$CHAIN" 2>/dev/null
iptables -t mangle -F "$CHAIN"
iptables -t mangle -C POSTROUTING -o "$IFACE" -j "$CHAIN" 2>/dev/null \
  || iptables -t mangle -A POSTROUTING -o "$IFACE" -j "$CHAIN"

echo
echo "=== Probing GTP-U header offset (needs the UE tunnel up) ==="
OFF=$(probe_offset)

if [ -z "$OFF" ]; then
  echo
  echo "!!! Could not determine the GTP-U inner-header offset." >&2
  echo "    The UE tunnel must be up and reachable for the probe to work:" >&2
  echo "      ping -I oaitun_ue1 -c1 $CORE_IP" >&2
  exit 1
fi

echo "  -> using inner-header offset $OFF"
echo
echo "=== Installing per-class transport marking ==="
build_iptables "$OFF"

echo
echo "=== Done. Current state ==="
tc -s class show dev "$IFACE" | grep -E 'class htb'
echo
iptables -t mangle -L "$CHAIN" -vn --line-numbers
echo
echo "Offset $OFF recorded for the controller."
echo "$OFF" > /run/ntn_gtp_offset
