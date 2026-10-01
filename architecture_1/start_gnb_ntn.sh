#!/bin/bash
# Architecture 1 NTN gNB -- the monolithic band254 LEO config used to
# collect the NTN half of the training set.
#
# This is deliberately the NON-E2 config. A1's data was collected without
# an E2 agent, and adding one changes CPU load and adds a 100 Hz
# indication path that was not present for the existing rows. Use
# ~/architecture_2/start_gnb_e2.sh only for A2 work.
#
# Before running, make sure nothing else holds the ports this needs:
#   RFsim 4043, N3 137.158.125.235:2152
# Another gNB left running will hold both and this will die with
#   [HW] could not open a socket ... RU 0: start_rf() ret -1
# Stop it first:  sudo pkill -f nr-softmodem
#
# Run as root:  sudo ~/architecture_1/start_gnb_ntn.sh
set -u

BUILD=/home/bluelabuser/openairinterface5g/cmake_targets/ran_build/build
CONF=/home/bluelabuser/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band254.u0.25prb.rfsim.ntn-leo.conf

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root: sudo $0" >&2
  exit 1
fi

[ -f "$CONF" ] || { echo "Missing config: $CONF" >&2; exit 1; }

# Fail early and legibly rather than 30 lines into an OAI assertion.
if ss -ltn 2>/dev/null | grep -q ':4043'; then
  echo "ERROR: RFsim port 4043 is already in use:" >&2
  ss -ltnp 2>/dev/null | grep ':4043' | sed 's/^/  /' >&2
  echo "  Stop the other gNB first: sudo pkill -f nr-softmodem" >&2
  exit 1
fi
if ss -lun 2>/dev/null | grep -q '137.158.125.235:2152'; then
  echo "ERROR: N3 port 2152 is already bound:" >&2
  ss -lunp 2>/dev/null | grep ':2152' | sed 's/^/  /' >&2
  echo "  Stop the other gNB first: sudo pkill -f nr-softmodem" >&2
  exit 1
fi

cd "$BUILD" || exit 1

exec stdbuf -oL ./nr-softmodem \
  -O "$CONF" \
  --gNBs.'[0]'.min_rxtxtime 6 \
  --rfsim
