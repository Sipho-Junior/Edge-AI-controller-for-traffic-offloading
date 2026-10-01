#!/bin/bash
# =================================================================
# Terrestrial baseline collection — 6 congestion levels
# With per-level HTB queue limits (bounded latency) and
# sleep between classes (clean state).
# 6 levels × 4 classes × 3 reps × 30 s = 72 runs
# =================================================================

CORE_IP="10.45.0.1"
IFACE="eno1"
DURATION=30
REPEATS=3
PING_INTERVAL=0.2
SLEEP_BETWEEN_CLASSES=3

UE_IP=$(ip -4 addr show oaitun_ue1 | grep -oP 'inet \K[0-9.]+')

CLASSES=(
  "Voice-EF:5201:100K"
  "Video-AF41:5202:1M"
  "Haptic-EF:5203:50K"
  "Background-BE:5204:500K"
)

# name:rate_cap:queue_limit (empty cap = Clear)
LEVELS=(
  "Terr-Clear::1000"
  "Terr-Moderate:4Mbit:200"
  "Terr-Heavy:2Mbit:100"
  "Terr-Stressed1:1Mbit:50"
  "Terr-Stressed2:500Kbit:30"
  "Terr-Stressed3:300Kbit:20"
)

BASE_DIR=~/architecture_1/terrestrial_data
N=1
while [ -d "${BASE_DIR}_${N}" ]; do N=$((N+1)); done
RESULTS_DIR="${BASE_DIR}_${N}"
mkdir -p "$RESULTS_DIR"

clean_qdiscs() { sudo tc qdisc del dev "$IFACE" root 2>/dev/null; }

apply_cap() {
  local cap="$1" qlimit="$2"
  clean_qdiscs
  if [ -n "$cap" ]; then
    sudo tc qdisc add dev "$IFACE" root handle 1: htb default 10
    sudo tc class add dev "$IFACE" parent 1: classid 1:10 htb rate "$cap" ceil "$cap"
    # Small sub-queue: bounded latency, drops excess rather than buffering
    sudo tc qdisc add dev "$IFACE" parent 1:10 handle 20: netem delay 0ms limit "$qlimit"
    echo "  [tc] HTB rate=$cap queue_limit=$qlimit pkts"
  else
    echo "  [tc] No cap (Clear)"
  fi
}

run_all_classes() {
  local label="$1" outfile="$2" tag="$3"
  echo "=== $label | UE=$UE_IP | $(date) ===" | tee -a "$outfile"
  for entry in "${CLASSES[@]}"; do
    IFS=':' read -r cname port rate <<< "$entry"
    echo "" | tee -a "$outfile"
    echo "--- Class: $cname | Port: $port | Rate: $rate ---" | tee -a "$outfile"

    local ping_log="${RESULTS_DIR}/${tag}_${cname}_ping.log"
    local ping_count=$((DURATION * 5 + 20))

    ping -I oaitun_ue1 -i "$PING_INTERVAL" -c "$ping_count" "$CORE_IP" > "$ping_log" 2>&1 &
    local ping_pid=$!

    iperf3 -c "$CORE_IP" -u -b "$rate" -l 50 -t "$DURATION" -p "$port" --forceflush 2>&1 | tee -a "$outfile"

    wait "$ping_pid" 2>/dev/null

    local rtt_line
    rtt_line=$(grep -E "rtt min/avg/max" "$ping_log" | tail -1)
    [ -n "$rtt_line" ] && echo "--- Ping summary ($cname): $rtt_line ---" | tee -a "$outfile"

    # Drain the queue between classes
    sleep "$SLEEP_BETWEEN_CLASSES"
  done
  echo "" | tee -a "$outfile"
}

echo ""
echo "===================================================="
echo ">>> TERRESTRIAL BASELINE (6 levels, bounded queues) <<<"
echo "Interface: $IFACE | Core: $CORE_IP | UE: $UE_IP"
echo "Duration: ${DURATION}s per run"
echo "Sleep between classes: ${SLEEP_BETWEEN_CLASSES}s"
echo "Results: $RESULTS_DIR"
echo "===================================================="
echo ""

for entry in "${LEVELS[@]}"; do
  IFS=':' read -r tname cap qlimit <<< "$entry"
  for rep in $(seq 1 "$REPEATS"); do
    echo ""
    echo ">>> Condition: $tname (cap='${cap:-none}' queue=$qlimit) rep=$rep/$REPEATS"
    apply_cap "$cap" "$qlimit"
    logfile="$RESULTS_DIR/${tname}_rep${rep}.log"
    tag="${tname}_rep${rep}"
    run_all_classes "${tname} rep=$rep" "$logfile" "$tag"
    sleep 2
  done
done

clean_qdiscs

echo ""
echo "=== TERRESTRIAL COLLECTION COMPLETE ==="
echo "Results in: $RESULTS_DIR"
ls -la "$RESULTS_DIR"
