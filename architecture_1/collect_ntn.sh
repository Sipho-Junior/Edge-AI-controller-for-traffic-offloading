#!/bin/bash
# =================================================================
# NTN baseline collection — 7 LEO profiles
# Interactive: prompts for UE restart between profiles/rep
# Auto-retry: if UE drops mid-rep, retries the rep once
# 7 profiles × 4 classes × 3 reps × 30 s = 84 runs
# =================================================================

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

PROFILES=(
  "NTN-Unimpaired:0ms:0ms:0%"
  "NTN-Best:20ms:2ms:0%"
  "NTN-Average:35ms:5ms:0%"
  "NTN-Worst:55ms:10ms:0%"
  "NTN-Worst-loss:55ms:10ms:0.5%"
  "NTN-Sev-loss:55ms:10ms:3%"
  "NTN-Voice-harsh:110ms:15ms:1%"
  # Intermittent profiles carry two extra fields: up_seconds:down_seconds.
  # Steady profiles above leave them empty and apply_ntn skips the cycle.
  # Values must match NTN_INTERMITTENCY in deploy_controller.py.
  "NTN-Intermittent:55ms:10ms:0%:18:2"
  "NTN-Intermittent-deep:55ms:10ms:0%:15:5"
)

BASE_DIR=~/architecture_1/ntn_data
N=1
while [ -d "${BASE_DIR}_${N}" ]; do N=$((N+1)); done
RESULTS_DIR="${BASE_DIR}_${N}"
mkdir -p "$RESULTS_DIR"

clean_qdiscs() { sudo tc qdisc del dev "$IFACE" root 2>/dev/null; }

# PID of the background outage cycle; empty when the link is steady.
INTERMITTENCY_PID=""

stop_intermittency() {
  if [ -n "$INTERMITTENCY_PID" ]; then
    kill "$INTERMITTENCY_PID" 2>/dev/null
    wait "$INTERMITTENCY_PID" 2>/dev/null
    INTERMITTENCY_PID=""
  fi
}

# Alternate the link between its profile and a total outage (netem
# loss 100%). This must match deploy_controller.py's _intermittency_loop
# exactly: collection and deployment have to emulate the same link, or
# the model is trained on one channel and steers another.
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
  if [ -n "$up_s" ] && [ -n "$down_s" ]; then
    run_intermittency "$delay" "$jitter" "$loss" "$up_s" "$down_s"
  fi
}

# Never leave an outage cycle toggling the link after this script exits.
trap 'stop_intermittency' INT TERM EXIT

# Returns 0 if tunnel up AND reachable.
#
# Retries rather than testing once. Under an intermittent profile the
# link is deliberately dead for up to 5 s at a time, so a single failed
# ping does not mean the UE has dropped -- it may simply have landed in
# an outage window. A single-shot check would report a false UE drop on
# roughly 10-25% of calls (the outage duty cycle), and with ~24 checks
# per profile that is a near-certain spurious abort.
#
# The retry window (~18 s worst case) comfortably exceeds the deepest
# outage, so only a genuine drop reaches `return 1`. Note this makes the
# negative case slow, which is fine: it only pays that cost when the UE
# really is down and a human has to intervene anyway.
ue_is_up() {
  ip -4 addr show oaitun_ue1 >/dev/null 2>&1 || return 1
  local tries=6
  for _ in $(seq 1 "$tries"); do
    ping -I oaitun_ue1 -c 1 -W 2 "$CORE_IP" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# Blocks until user confirms UE is up
wait_for_ue() {
  while ! ue_is_up; do
    echo ""
    echo "===================================================="
    echo ">>> UE TUNNEL IS DOWN <<<"
    echo ">>> Restart the UE in Terminal 2 with: <<<"
    echo ""
    echo "cd ~/openairinterface5g/cmake_targets/ran_build/build"
    echo "sudo ./nr-uesoftmodem -O ../../../targets/PROJECTS/GENERIC-NR-5GC/CONF/ue.conf \\"
    echo "  --band 254 -C 2488400000 --CO -873500000 \\"
    echo "  -r 25 --numerology 0 --ssb 60 \\"
    echo "  --rfsim --rfsimulator.[0].prop_delay 20 --rfsimulator.[0].options chanmod \\"
    echo "  --time-sync-I 0.1 --ntn-initial-time-drift -46 \\"
    echo "  --initial-fo 57340 --cont-fo-comp 2"
    echo ""
    echo ">>> Press ENTER here once oaitun_ue1 is up and ping works. <<<"
    echo "===================================================="
    read -r _
  done
  echo ">>> UE tunnel is UP. Proceeding..."
  ip -4 addr show oaitun_ue1 | grep inet
}

run_all_classes() {
  local label="$1" outfile="$2" tag="$3"
  echo "=== $label | $(date) ===" | tee -a "$outfile"
  for entry in "${CLASSES[@]}"; do
    IFS=':' read -r cname port rate <<< "$entry"
    echo "" | tee -a "$outfile"
    echo "--- Class: $cname | Port: $port | Rate: $rate ---" | tee -a "$outfile"

    # Pre-check: UE must be up before we start this class
    if ! ue_is_up; then
      echo "!!! UE dropped before $cname — aborting rep." | tee -a "$outfile"
      return 1
    fi

    local ping_log="${RESULTS_DIR}/${tag}_${cname}_ping.log"
    local ping_count=$((DURATION * 5 + 20))

    ping -I oaitun_ue1 -i "$PING_INTERVAL" -c "$ping_count" "$CORE_IP" > "$ping_log" 2>&1 &
    local ping_pid=$!

    iperf3 -c "$CORE_IP" -u -b "$rate" -l 50 -t "$DURATION" -p "$port" --forceflush 2>&1 | tee -a "$outfile"

    wait "$ping_pid" 2>/dev/null

    # Post-check: if UE died during the run, log it
    if ! ue_is_up; then
      echo "!!! UE dropped during $cname." | tee -a "$outfile"
      return 1
    fi

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
echo ">>> NTN BASELINE (interactive, 7 LEO profiles) <<<"
echo "UE will disconnect every ~5 min. Restart it when prompted."
echo "Results: $RESULTS_DIR"
echo "===================================================="
echo ""

for entry in "${PROFILES[@]}"; do
  IFS=':' read -r pname delay jitter loss up_s down_s <<< "$entry"

  echo ""
  echo "===================================================="
  echo ">>> PROFILE: $pname (delay=$delay jitter=$jitter loss=$loss)"
  echo "===================================================="

  # Ensure UE is healthy before starting the profile
  wait_for_ue
  apply_ntn "$delay" "$jitter" "$loss" "$up_s" "$down_s"

  for rep in $(seq 1 "$REPEATS"); do
    echo ""
    echo ">>> $pname rep=$rep/$REPEATS"
    logfile="$RESULTS_DIR/${pname}_rep${rep}.log"
    tag="${pname}_rep${rep}"

    if ! run_all_classes "${pname} rep=$rep" "$logfile" "$tag"; then
      echo ""
      echo ">>> Profile $pname rep $rep interrupted — restart UE."
      wait_for_ue
      apply_ntn "$delay" "$jitter" "$loss" "$up_s" "$down_s"
      echo ">>> Retrying $pname rep=$rep..."
      run_all_classes "${pname} rep=$rep (retry)" "$logfile" "$tag"
    fi

    # Check UE before next rep — restart if it died
    if [ "$rep" -lt "$REPEATS" ]; then
      if ! ue_is_up; then
        echo ""
        echo ">>> UE down before rep $((rep+1)). Prompting for restart..."
        wait_for_ue
        apply_ntn "$delay" "$jitter" "$loss" "$up_s" "$down_s"
      fi
    fi

    sleep 2
  done

  # Stop the cycle BEFORE removing the qdisc, or the toggler keeps
  # issuing `tc qdisc change` against a root that no longer exists until
  # the next profile stops it.
  stop_intermittency
  clean_qdiscs
  echo ""
  echo ">>> Profile $pname complete."
done

clean_qdiscs
echo ""
echo "=== NTN DATA COLLECTION COMPLETE ==="
echo "Results in: $RESULTS_DIR"
ls -la "$RESULTS_DIR"
