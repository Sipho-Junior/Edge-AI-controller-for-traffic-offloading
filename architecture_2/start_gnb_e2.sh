#!/bin/bash
# Architecture 2 gNB: identical to the A1 launch except for the config
# file, which adds the e2_agent block pointing at the FlexRIC Near-RT RIC.
# Run as root:  sudo ~/architecture_2/start_gnb_e2.sh
set -u

BUILD=/home/bluelabuser/openairinterface5g/cmake_targets/ran_build/build
CONF=/home/bluelabuser/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band254.u0.25prb.rfsim.ntn-leo.e2.conf

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root: sudo $0" >&2
  exit 1
fi

[ -f "$CONF" ] || { echo "Missing config: $CONF" >&2; exit 1; }

cd "$BUILD" || exit 1

exec ./nr-softmodem \
  -O "$CONF" \
  --gNBs.'[0]'.min_rxtxtime 6 \
  --rfsim
