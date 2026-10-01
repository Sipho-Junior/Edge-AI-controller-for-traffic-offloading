#!/bin/bash
CORE_IP="10.45.0.1"
IFACE="eno1"
DURATION=30
REPEATS=3
RESULTS_DIR=~/architecture_1/ntn_data_3

# Only Video-AF41, only NTN-Sev-loss
PROFILE_NAME="NTN-Sev-loss"
DELAY="55ms"; JITTER="10ms"; LOSS="3%"
PORT=5202; RATE="1M"

clean_qdiscs() { sudo tc qdisc del dev "$IFACE" root 2>/dev/null; }

ue_is_up() {
  ip -4 addr show oaitun_ue1 >/dev/null 2>&1 || return 1
  ping -I oaitun_ue1 -c 1 -W 2 "$CORE_IP" >/dev/null 2>&1 || return 1
  return 0
}

wait_for_ue() {
  while ! ue_is_up; do
    echo ">>> UE DOWN. Restart and press ENTER."
    read -r _
  done
  echo ">>> UE UP."
}

echo ">>> Collecting missing cell: Video-AF41 + NTN-Sev-loss"
clean_qdiscs
sudo tc qdisc add dev "$IFACE" root netem delay "$DELAY" "$JITTER" loss "$LOSS"
echo "  [tc] netem delay=$DELAY jitter=$JITTER loss=$LOSS"

for rep in 1 2 3; do
  echo ""
  echo ">>> Video-AF41 + NTN-Sev-loss rep=$rep/3"

  # Don't overwrite existing logs — append to same file
  main_log="${RESULTS_DIR}/${PROFILE_NAME}_rep${rep}.log"
  ping_log="${RESULTS_DIR}/${PROFILE_NAME}_rep${rep}_Video-AF41_ping.log"

  if ! ue_is_up; then
    echo ">>> UE DOWN. Restart and press ENTER."
    read -r _
    wait_for_ue
  fi

  echo "" | tee -a "$main_log"
  echo "--- Class: Video-AF41 | Port: $PORT | Rate: $RATE (retry) ---" | tee -a "$main_log"

  ping -I oaitun_ue1 -i 0.2 -c 170 "$CORE_IP" > "$ping_log" 2>&1 &
  PING_PID=$!

  iperf3 -c "$CORE_IP" -u -b "$RATE" -l 50 -t "$DURATION" -p "$PORT" --forceflush 2>&1 | tee -a "$main_log"

  wait "$PING_PID" 2>/dev/null
  rtt_line=$(grep -E "rtt min/avg/max" "$ping_log" | tail -1)
  [ -n "$rtt_line" ] && echo "--- Ping summary (Video-AF41): $rtt_line ---" | tee -a "$main_log"

  sleep 3
done

clean_qdiscs
echo ""
echo "=== Done. Now re-run parse_ntn.py and label_ntn.py ==="
