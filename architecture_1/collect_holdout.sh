#!/bin/bash
# =================================================================
# collect_holdout.sh -- independent hold-out batch for repeatability
#
# Re-measures a SMALL subset of NTN profiles in a fresh session, so the
# trained model can be tested against data collected on a different day,
# with different UE instances and different system state. Every existing
# validation holds out conditions from WITHIN one collection session;
# this holds out the session itself.
#
# SAFETY: writes to ~/architecture_1/holdout_batch/, which deliberately
# does NOT match the ntn_data_[0-9]* glob that merge_dataset.py's latest()
# globs for. If this batch were written to ntn_data_5, re-running the
# pipeline would silently rebuild master_metrics.csv from THREE conditions
# instead of nine and retrain on it. The name is the guard.
#
# Nothing here touches training_data/. Analysis is compare_holdout.py.
#
# Run as the NORMAL USER (it calls sudo internally for tc):
#   ~/architecture_1/collect_holdout.sh
# =================================================================

CORE_IP="10.45.0.1"
IFACE="eno1"
DURATION=30
REPEATS=2
PING_INTERVAL=0.2
SLEEP_BETWEEN_CLASSES=3

RESULTS_DIR=~/architecture_1/holdout_batch
mkdir -p "$RESULTS_DIR"

CLASSES=(
  "Voice-EF:5201:100K"
  "Video-AF41:5202:1M"
  "Haptic-EF:5203:50K"
  "Background-BE:5204:500K"
)

# Three profiles, chosen for what each tests rather than for coverage:
#   Unimpaired    sanity -- should reproduce exactly
#   Sev-loss      weakest generalisation fold (0.958) and the condition
#                 carrying the QoS margin; most worth re-measuring
#   Intermittent  newest profile, and the one whose measurement quantises
#                 against the 20 s outage cycle
PROFILES=(
  "NTN-Unimpaired:0ms:0ms:0%"
  "NTN-Sev-loss:55ms:10ms:3%"
  "NTN-Intermittent:55ms:10ms:0%:18:2"
)

clean_qdiscs() { sudo tc qdisc del dev "$IFACE" root 2>/dev/null; }

INTERMITTENCY_PID=""
stop_intermittency() {
  if [ -n "$INTERMITTENCY_PID" ]; then
    kill "$INTERMITTENCY_PID" 2>/dev/null
    wait "$INTERMITTENCY_PID" 2>/dev/null
    INTERMITTENCY_PID=""
  fi
}
run_intermittency() {
  local delay="$1" jitter="$2" loss="$3" up_s="$4" down_s="$5"
  (
    while true; do
      sleep "$up_s"
      sudo tc qdisc change dev "$IFACE" root netem delay "$delay" "$jitter" loss 100% 2>/dev/null
      sleep "$down_s"
      sudo tc qdisc change dev "$IFACE" root netem delay "$delay" "$jitter" loss "$loss" 2>/dev/null
    done
  ) &
  INTERMITTENCY_PID=$!
  echo "  [tc] intermittency: ${down_s}s outage every $((up_s + down_s))s"
}
apply_ntn() {
  local delay="$1" jitter="$2" loss="$3" up_s="${4:-}" down_s="${5:-}"
  stop_intermittency
  clean_qdiscs
  sudo tc qdisc add dev "$IFACE" root netem delay "$delay" "$jitter" loss "$loss"
  echo "  [tc] netem delay=$delay jitter=$jitter loss=$loss"
  [ -n "$up_s" ] && [ -n "$down_s" ] && run_intermittency "$delay" "$jitter" "$loss" "$up_s" "$down_s"
}
trap 'stop_intermittency' INT TERM EXIT

# Retries rather than testing once: under an intermittent profile the link
# is deliberately dead for up to 5 s, so a single failed ping does not mean
# the UE has dropped.
ue_is_up() {
  ip -4 addr show oaitun_ue1 >/dev/null 2>&1 || return 1
  for _ in $(seq 1 6); do
    ping -I oaitun_ue1 -c 1 -W 2 "$CORE_IP" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

wait_for_ue() {
  local waited=0
  while ! ue_is_up; do
    if [ "$waited" -ge 420 ]; then
      echo "  !! UE did not recover in 420s -- aborting." ; return 1
    fi
    echo "  .. UE down, waiting for ue_supervisor.sh (${waited}s)"
    sleep 15; waited=$((waited + 15))
  done
  return 0
}

run_all_classes() {
  local label="$1" outfile="$2" tag="$3"
  echo "=== $label | $(date) ===" | tee -a "$outfile"
  for entry in "${CLASSES[@]}"; do
    IFS=':' read -r cname port rate <<< "$entry"
    echo "" | tee -a "$outfile"
    echo "--- Class: $cname | Port: $port | Rate: $rate ---" | tee -a "$outfile"
    ue_is_up || { echo "!!! UE dropped before $cname." | tee -a "$outfile"; return 1; }

    local ping_log="${RESULTS_DIR}/${tag}_${cname}_ping.log"
    ping -I oaitun_ue1 -i "$PING_INTERVAL" -c $((DURATION * 5)) -W 1 "$CORE_IP" > "$ping_log" 2>&1 &
    local ping_pid=$!
    iperf3 -c "$CORE_IP" -u -b "$rate" -l 50 -t "$DURATION" -p "$port" --forceflush 2>&1 | tee -a "$outfile"
    wait "$ping_pid" 2>/dev/null

    ue_is_up || { echo "!!! UE dropped during $cname." | tee -a "$outfile"; return 1; }
    local rtt_line
    rtt_line=$(grep -E "rtt min/avg/max" "$ping_log" | tail -1)
    [ -n "$rtt_line" ] && echo "--- Ping summary ($cname): $rtt_line ---" | tee -a "$outfile"
    sleep "$SLEEP_BETWEEN_CLASSES"
  done
  echo "" | tee -a "$outfile"
  return 0
}

echo ""
echo "===================================================="
echo ">>> HOLD-OUT BATCH  (${#PROFILES[@]} profiles x 4 classes x $REPEATS reps)"
echo "Results: $RESULTS_DIR   (NOT ntn_data_* -- pipeline cannot pick it up)"
echo "Est. ~25 min. Run ue_supervisor.sh alongside."
echo "===================================================="

for entry in "${PROFILES[@]}"; do
  IFS=':' read -r pname delay jitter loss up_s down_s <<< "$entry"
  echo ""
  echo ">>> PROFILE: $pname (delay=$delay jitter=$jitter loss=$loss)"
  wait_for_ue || exit 1
  apply_ntn "$delay" "$jitter" "$loss" "$up_s" "$down_s"

  for rep in $(seq 1 "$REPEATS"); do
    echo ""
    echo ">>> $pname rep=$rep/$REPEATS"
    logfile="$RESULTS_DIR/${pname}_rep${rep}.log"
    tag="${pname}_rep${rep}"
    if ! run_all_classes "${pname} rep=$rep" "$logfile" "$tag"; then
      echo ">>> interrupted -- waiting for UE, then retrying"
      wait_for_ue || exit 1
      apply_ntn "$delay" "$jitter" "$loss" "$up_s" "$down_s"
      run_all_classes "${pname} rep=$rep (retry)" "$logfile" "$tag"
    fi
    sleep 2
  done
  stop_intermittency
  clean_qdiscs
  echo ">>> Profile $pname complete."
done

clean_qdiscs
echo ""
echo "=== HOLD-OUT COLLECTION COMPLETE ==="
echo "Next: python3 ~/architecture_1/compare_holdout.py"
ls -la "$RESULTS_DIR" | head -5
