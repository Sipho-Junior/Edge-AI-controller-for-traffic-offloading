#!/bin/bash
# Architecture 2 UE: unchanged from A1 -- the UE has no E2 agent, only
# the gNB does. Kept here so both sides launch from one place.
# Run as root:  sudo ~/architecture_2/start_ue.sh
set -u

BUILD=/home/bluelabuser/openairinterface5g/cmake_targets/ran_build/build
CONF=/home/bluelabuser/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/ue.conf

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root: sudo $0" >&2
  exit 1
fi

cd "$BUILD" || exit 1

exec ./nr-uesoftmodem \
  -O "$CONF" \
  --band 254 -C 2488400000 --CO -873500000 \
  -r 25 --numerology 0 --ssb 60 \
  --rfsim \
  --rfsimulator.'[0]'.prop_delay 20 \
  --rfsimulator.'[0]'.options chanmod \
  --time-sync-I 0.1 \
  --ntn-initial-time-drift -46 \
  --initial-fo 57340 \
  --cont-fo-comp 2
