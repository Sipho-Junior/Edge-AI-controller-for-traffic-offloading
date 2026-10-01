#!/bin/bash
# Launch the A2 ML xApp.
#
# LD_PRELOAD is required: libe42_xapp_shared.so references
# asn_DEF_NR_UL_DCCH_Message, which FlexRIC only provides as a static
# archive (libasn1_nr_rrc.a). The C xApps link it directly; the Python
# bindings load it as a shared object and cannot resolve it without this.
#
# Runs as the normal user -- it only talks to the RIC and writes to a
# socket. Only the steering daemon needs root.
set -u

export LD_PRELOAD=/home/bluelabuser/architecture_2/libasn1_nr_rrc.so
export XAPP_DURATION="${XAPP_DURATION:-60}"
export CONTROL_PERIOD="${CONTROL_PERIOD:-1.0}"
export DWELL_SEC="${DWELL_SEC:-0}"

exec python3 /home/bluelabuser/architecture_2/ml_xapp.py "$@"
