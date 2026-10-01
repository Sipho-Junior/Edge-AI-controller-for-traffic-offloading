#!/bin/bash
# Collect only NTN-Voice-harsh into an existing ntn_data folder
CORE_IP="10.45.0.1"
IFACE="eno1"
DURATION=30
REPEATS=3
PING_INTERVAL=0.2
SLEEP_BETWEEN_CLASSES=3

CLASSES=(
  "Voice-EF:5201:100K"
  "Video-AF41:5202:1M"
  "Haptic-EF:5203:50K"
  "Background-BE:5204:500K"
)

# Point to the existing folder
RESULTS_DIR=~/architecture_1/ntn_data_3
PROFILE_NAME="NTN-Voice-harsh"
DELAY="110ms"
JITTER="15ms"
LOSS="1%"

clean_qdiscs() { sudo tc qdisc del dev "$IFACE" root 2>/dev/null; }
apply_ntn() {
  clean_qdiscs
  sudo tc qdisc add dev "$IFACE" root netem delay "$DELAY" "$JITTER" loss "$LOSS"
  echo "  [tc] netem delay=$DELAY jitter=$JITTER loss=$LOSS"
}

ue_is_up() {
  ip -4 addr show oaitun_ue1 >/dev/null 2>&1 || return 1
  ping -I oaitun_ue1 -c 1 -W 2 "$CORE_IP" >/dev/null 2>&1 || return 1
  return 0
}

wait_for_ue() {
  while ! ue_is_up; do
    echo ">>> UE TUNNEL IS DOWN. Restart the UE and press ENTER."
    read -r _
  done
  echo ">>> UE tunnel UP."
  ip -4 addr show oaitun_ue1 | grep inet
}

run_all_classes() {
  local label="$1" outfile="$2" tag="$3"
  echo "=== $label | $(date) ===" | tee -a "$outfile"
  for entry in "${CLASSES[@]}"; do
    IFS=':' read -r cname port rate <<< "$entry"
    echo "" | tee -a "$outfile"
    echo "--- Class: $cname | Port: $port | Rate: $rate ---" | tee -a "$outfile"
    if ! ue_is_up; then
      echo "!!! UE dropped before $cname" | tee -a "$outfile"
      return 1
    fi
    local ping_log="${RESULTS_DIR}/${tag}_${cname}_ping.log"
    local ping_count=$((DURATION * 5 + 20))
    ping -I oaitun_ue1 -i "$PING_INTERVAL" -c "$ping_count" "$CORE_IP" > "$ping_log" 2>&1 &
    local ping_pid=$!
    iperf3 -c "$CORE_IP" -u -b "$rate" -l 50 -t "$DURATION" -p "$port" --forceflush 2>&1 | tee -a "$outfile"
    wait "$ping_pid" 2>/dev/null
    local rtt_line
    rtt_line=$(grep -E "rtt min/avg/max" "$ping_log" | tail -1)
    [ -n "$rtt_line" ] && echo "--- Ping summary ($cname): $rtt_line ---" | tee -a "$outfile"
    sleep "$SLEEP_BETWEEN_CLASSES"
  done
  echo "" | tee -a "$outfile"
  return 0
}

echo ""
echo ">>> Collecting profile $PROFILE_NAME into $RESULTS_DIR <<<"

wait_for_ue
apply_ntn

for rep in $(seq 1 "$REPEATS"); do
  echo ""
  echo ">>> $PROFILE_NAME rep=$rep/$REPEATS"
  logfile="$RESULTS_DIR/${PROFILE_NAME}_rep${rep}.log"
  tag="${PROFILE_NAME}_rep${rep}"

  if ! run_all_classes "$PROFILE_NAME rep=$rep" "$logfile" "$tag"; then
    echo ">>> Interrupted. Restart UE and press ENTER."
    wait_for_ue
    apply_ntn
    run_all_classes "$PROFILE_NAME rep=$rep (retry)" "$logfile" "$tag"
  fi

  if [ "$rep" -lt "$REPEATS" ]; then
    if ! ue_is_up; then
      echo ">>> UE down before rep $((rep+1)). Restart and press ENTER."
      wait_for_ue
      apply_ntn
    fi
  fi
  sleep 2
done

clean_qdiscs
echo ""
echo "=== $PROFILE_NAME COMPLETE ==="
ls -la "$RESULTS_DIR" | grep "$PROFILE_NAME"
