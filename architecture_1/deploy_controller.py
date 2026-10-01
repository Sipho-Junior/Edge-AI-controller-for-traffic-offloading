#!/usr/bin/env python3
"""
Harness-driven AI controller for TN/NTN traffic steering.

Reads the current condition from /tmp/current_condition.txt, queries the
trained pipeline model, updates tc filter rules to route each traffic class
to the appropriate backhaul path.

The model is a scikit-learn Pipeline (ColumnTransformer + DecisionTree),
fitted on features that are all observable at runtime:
    class, terr_condition, ntn_condition
    terr_rtt_ms, terr_loss, terr_thr_kbps
    ntn_rtt_ms, ntn_loss, ntn_thr_kbps
"""
import os
import time
import pickle
import subprocess
import sys
import threading

import numpy as np
import pandas as pd

# --------------------------------------------------------------- config ----
MODEL_PATH = os.path.expanduser("~/architecture_1/training_data/model.pkl")
METRICS_PATH = os.path.expanduser("~/architecture_1/training_data/master_metrics.csv")
CONDITION_FILE = "/tmp/current_condition.txt"
IFACE = "eno1"
LOOP_INTERVAL = 1.0

# Outer-header codepoint per class, stamped by the NTNMARK mangle chain
# (see setup_steering.sh). These are TRANSPORT markings on the GTP-U
# outer header, not the application's own DSCP: the app keeps its Table
# 1.3 marking in the inner header, which tc cannot see.
#
# Voice-EF and Haptic-EF both carry application DSCP EF (0x2e) but hold
# different 5QI (1 vs 87) and different PDBs (100 ms vs 5 ms), and the
# labelled data routes them differently in 8 of 42 condition pairs.
# They therefore need distinct outer codepoints to be steerable apart.
DSCP = {
    "Voice-EF": 0x2e,      # EF
    "Video-AF41": 0x22,    # AF41
    "Haptic-EF": 0x28,     # CS5 -- distinct from Voice
    "Background-BE": 0x08, # CS1 -- distinct from unmarked host traffic
}

# tc filter priority per codepoint. One priority per class, so no class
# can overwrite another's filter.
DSCP_PRIO = {
    0x2e: 5,   # Voice
    0x22: 6,   # Video
    0x28: 7,   # Haptic
    0x08: 8,   # Background
}

# tc class IDs on the HTB root qdisc. 1:99 is unshaped and carries
# everything that is not classified user-plane traffic (SSH, NGAP,
# RFSim); it is the htb default and must never be impaired.
CLASS_TERRESTRIAL = "1:10"
CLASS_NTN = "1:20"

# NetEm impairment per NTN condition, matching collect_ntn.sh.
NTN_PROFILES = {
    "Unimpaired":  ("0ms",   "0ms",  "0%"),
    "Best":        ("20ms",  "2ms",  "0%"),
    "Average":     ("35ms",  "5ms",  "0%"),
    "Worst":       ("55ms",  "10ms", "0%"),
    "Worst-loss":  ("55ms",  "10ms", "0.5%"),
    "Sev-loss":    ("55ms",  "10ms", "3%"),
    "Voice-harsh": ("110ms", "15ms", "1%"),
    # Intermittent profiles. The steady-state impairment is the same as
    # "Worst"; what distinguishes them is the outage cycle in
    # NTN_INTERMITTENCY below. JBM7 names intermittent connectivity as a
    # backhaul characteristic to emulate alongside constrained bandwidth
    # and elevated latency, and it is the condition a fixed class rule
    # cannot answer: a rule that sends eMBB to NTN on class alone keeps
    # sending it into a link that is not there.
    "Intermittent":      ("55ms", "10ms", "0%"),
    "Intermittent-deep": ("55ms", "10ms", "0%"),
}

# condition -> (up_seconds, down_seconds). Absent or None means a steady
# link. During a down window the NTN class is set to 100% loss, which is
# how netem expresses "the link is gone" -- the qdisc stays in place so
# the outage is a property of the path, not a teardown of the datapath.
#
# Kept in a SEPARATE dict rather than widening NTN_PROFILES to 4-tuples:
# deploy_controller.py:312 and steer_daemon.py:162 both unpack it as
# `d, j, l = NTN_PROFILES[...]`, and widening the tuple would break both.
#
# Duty cycles are motivated by LEO handover/blockage gaps, which are
# seconds rather than minutes. Two depths so the effect can be shown as a
# trend rather than a single point.
NTN_INTERMITTENCY = {
    "Intermittent":      (18.0, 2.0),   # 2 s outage every 20 s -- 10% down
    "Intermittent-deep": (15.0, 5.0),   # 5 s outage every 20 s -- 25% down
}

# Terrestrial rate cap and queue limit per congestion level, matching
# collect_terrestrial.sh. None = uncapped.
TERR_PROFILES = {
    "Clear":     (None,      1000),
    "Moderate":  ("4Mbit",    200),
    "Heavy":     ("2Mbit",    100),
    "Stressed1": ("1Mbit",     50),
    "Stressed2": ("500Kbit",   30),
    "Stressed3": ("300Kbit",   20),
}


# ------------------------------------------------------------ artifacts ----
def load_artifacts():
    with open(MODEL_PATH, "rb") as f:
        artifacts = pickle.load(f)

    pipe = artifacts["pipeline"]
    cat_cols = artifacts["cat_cols"]
    num_cols = artifacts["num_cols"]

    df = pd.read_csv(METRICS_PATH)
    lookup = {}
    for _, row in df.iterrows():
        key = (row["class"], row["condition"], row["path"])
        lookup[key] = {
            "rtt_ms": float(row["rtt_ms"]),
            "loss_fraction": float(row["loss_fraction"]),
            "throughput_kbps": float(row["throughput_kbps"]),
        }

    print(f"Loaded pipeline model and {len(lookup)} metric lookups")
    return pipe, cat_cols, num_cols, lookup


def read_current_condition():
    try:
        with open(CONDITION_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


# ------------------------------------------------------------ inference ----
def build_features(cat_cols, num_cols, lookup,
                   traffic_class, terr_condition, ntn_condition):
    t_key = (traffic_class, terr_condition, "Terrestrial")
    n_key = (traffic_class, ntn_condition, "NTN")
    if t_key not in lookup or n_key not in lookup:
        return None, False

    t = lookup[t_key]
    n = lookup[n_key]

    row = {
        "class": traffic_class,
        "terr_condition": terr_condition,
        "ntn_condition": ntn_condition,
        "terr_rtt_ms": t["rtt_ms"],
        "terr_loss": t["loss_fraction"],
        "terr_thr_kbps": t["throughput_kbps"],
        "ntn_rtt_ms": n["rtt_ms"],
        "ntn_loss": n["loss_fraction"],
        "ntn_thr_kbps": n["throughput_kbps"],
    }
    df_row = pd.DataFrame([row])
    return df_row, True


def decide(pipe, df_row):
    pred = int(pipe.predict(df_row)[0])
    return "NTN" if pred == 1 else "Terrestrial"


# ------------------------------------------------------------------- tc ----
def update_tc_filter(dscp, target_path):
    """
    Add or replace the tc filter for the given DSCP.

    DSCP lives in the top 6 bits of the ToS byte. The tc 'u32' classifier
    matches at a 32-bit offset, so the ToS byte sits at bits 16-23 of the
    first word -- hence the (dscp << 2) shift.

    The mask is 0xfc, not 0xff: the low two bits of the ToS byte are ECN,
    which the transport may set independently. Masking them in would make
    the filter miss every ECN-marked packet.
    """
    flowid = CLASS_NTN if target_path == "NTN" else CLASS_TERRESTRIAL
    prio = DSCP_PRIO.get(dscp)
    if prio is None:
        return False

    tos = dscp << 2

    # Delete only this priority (quiet if it doesn't exist yet).
    subprocess.run(
        ["sudo", "tc", "filter", "del", "dev", IFACE,
         "parent", "1:", "protocol", "ip", "prio", str(prio)],
        capture_output=True, text=True,
    )

    res = subprocess.run(
        ["sudo", "tc", "filter", "add", "dev", IFACE,
         "parent", "1:", "protocol", "ip", "prio", str(prio), "u32",
         "match", "ip", "tos", hex(tos), "0xfc",
         "flowid", flowid],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print(f"  [tc error] {res.stderr.strip()}")
        return False
    return True


def _set_ntn_netem(delay, jitter, loss):
    """Write one netem state onto the NTN class. Returns True on success."""
    res = subprocess.run(
        ["sudo", "tc", "qdisc", "change", "dev", IFACE,
         "parent", CLASS_NTN, "handle", "20:", "netem",
         "delay", delay, jitter, "loss", loss, "limit", "1000"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print(f"  [netem error] {res.stderr.strip()}")
        return False
    return True


# Handle for the running outage cycle. Module-level so that A1's
# controller and A2's steering daemon -- which imports this module --
# share one cycle and can never start two.
_intermittency = {"thread": None, "stop": None, "condition": None}


def _intermittency_loop(stop_evt, delay, jitter, loss, up_s, down_s):
    """Alternate the NTN class between its profile and a total outage.

    stop_evt.wait() is used rather than time.sleep() so that a condition
    change takes effect immediately instead of after the remainder of the
    current window -- otherwise a scripted sweep would spend up to
    `up_s + down_s` seconds measuring the previous condition.
    """
    while not stop_evt.is_set():
        if stop_evt.wait(up_s):
            break
        if stop_evt.is_set():
            break
        _set_ntn_netem(delay, jitter, "100%")      # outage
        if stop_evt.wait(down_s):
            break
        _set_ntn_netem(delay, jitter, loss)        # restored


def stop_intermittency():
    """Stop any running outage cycle and leave the link in its up state."""
    if _intermittency["thread"] is not None:
        _intermittency["stop"].set()
        _intermittency["thread"].join(timeout=2.0)
    _intermittency["thread"] = None
    _intermittency["stop"] = None
    _intermittency["condition"] = None


def start_intermittency(ntn_condition):
    """Start the outage cycle for a condition, if it has one.

    Idempotent: re-applying the same condition does not restart the
    cycle, so a controller that re-reads an unchanged condition file does
    not keep resetting the outage phase.
    """
    cycle = NTN_INTERMITTENCY.get(ntn_condition)
    if cycle is None:
        stop_intermittency()
        return False
    if _intermittency["condition"] == ntn_condition:
        return True

    stop_intermittency()
    delay, jitter, loss = NTN_PROFILES[ntn_condition]
    up_s, down_s = cycle
    stop_evt = threading.Event()
    t = threading.Thread(
        target=_intermittency_loop,
        args=(stop_evt, delay, jitter, loss, up_s, down_s),
        daemon=True,
    )
    _intermittency.update({"thread": t, "stop": stop_evt,
                           "condition": ntn_condition})
    t.start()
    print(f"  [intermittency] {ntn_condition}: {down_s}s outage every "
          f"{up_s + down_s}s ({100.0 * down_s / (up_s + down_s):.0f}% down)")
    return True


def apply_conditions(terr_condition, ntn_condition):
    """
    Put the emulated links into the state the condition file names.

    Without this the controller reads a condition, predicts against the
    metrics for it, and then measures against whatever impairment
    happened to be left on the interface -- so every condition would be
    evaluated over the same pipe.
    """
    ok = True

    ntn = NTN_PROFILES.get(ntn_condition)
    if ntn is None:
        print(f"  [cond] unknown NTN condition {ntn_condition!r}")
        ok = False
    else:
        delay, jitter, loss = ntn
        # Stop any previous outage cycle BEFORE writing the new steady
        # state, or a toggle still in flight can overwrite it and leave
        # the link emulating the condition we just moved away from.
        stop_intermittency()
        if not _set_ntn_netem(delay, jitter, loss):
            ok = False
        # Restart the cycle only if this condition has one; steady
        # conditions are left alone by start_intermittency().
        start_intermittency(ntn_condition)

    terr = TERR_PROFILES.get(terr_condition)
    if terr is None:
        print(f"  [cond] unknown terrestrial condition {terr_condition!r}")
        return False

    cap, qlimit = terr
    rate = cap if cap is not None else "1Gbit"
    res = subprocess.run(
        ["sudo", "tc", "class", "change", "dev", IFACE,
         "parent", "1:", "classid", CLASS_TERRESTRIAL, "htb",
         "rate", rate, "ceil", rate],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print(f"  [htb error] {res.stderr.strip()}")
        ok = False

    res = subprocess.run(
        ["sudo", "tc", "qdisc", "change", "dev", IFACE,
         "parent", CLASS_TERRESTRIAL, "handle", "10:", "netem",
         "delay", "0ms", "limit", str(qlimit)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print(f"  [netem error] {res.stderr.strip()}")
        ok = False

    return ok


def ensure_qdisc_setup():
    res = subprocess.run(
        ["sudo", "tc", "qdisc", "show", "dev", IFACE],
        capture_output=True, text=True,
    )
    if "htb" not in res.stdout:
        print(f"WARNING: htb qdisc not found on {IFACE}.")
        print(f"  {res.stdout.strip()}")
        print("Run setup_steering.sh before starting the controller.")
        return False

    # Without the mangle chain the GTP-U outer header carries no class
    # marking and every user packet falls through to the default class,
    # so the controller's decisions would have no effect on any packet.
    res = subprocess.run(
        ["sudo", "iptables", "-t", "mangle", "-L", "NTNMARK", "-n"],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or "DSCP" not in res.stdout:
        print("WARNING: mangle chain NTNMARK is missing or empty.")
        print("  User-plane traffic crosses eno1 GTP-U-encapsulated, so the")
        print("  inner DSCP is invisible to tc. Run setup_steering.sh first.")
        return False
    return True


# ------------------------------------------------------------------ main ----
def main():
    pipe, cat_cols, num_cols, lookup = load_artifacts()

    if not ensure_qdisc_setup():
        sys.exit(1)

    classes = ["Voice-EF", "Video-AF41", "Haptic-EF", "Background-BE"]

    print()
    print("=" * 62)
    print("AI CONTROLLER RUNNING")
    print(f"  Model   : {MODEL_PATH}")
    print(f"  Metrics : {METRICS_PATH}")
    print(f"  Condition file: {CONDITION_FILE}")
    print(f"  Interface: {IFACE}")
    print(f"  Loop interval: {LOOP_INTERVAL}s")
    print("=" * 62)
    print()

    last_condition = None
    last_decision = {}

    while True:
        condition = read_current_condition()

        if condition is None:
            print(f"[{time.strftime('%H:%M:%S')}] Waiting for {CONDITION_FILE}...")
            time.sleep(LOOP_INTERVAL)
            continue

        try:
            terr_cond, ntn_cond = condition.split(":", 1)
        except ValueError:
            print(f"[{time.strftime('%H:%M:%S')}] Invalid condition: {condition}")
            time.sleep(LOOP_INTERVAL)
            continue

        if condition != last_condition:
            print(f"[{time.strftime('%H:%M:%S')}] Condition: terr={terr_cond} ntn={ntn_cond}")
            if apply_conditions(terr_cond, ntn_cond):
                terr_cap = TERR_PROFILES[terr_cond][0] or "uncapped"
                d, j, l = NTN_PROFILES[ntn_cond]
                print(f"  [links] terr={terr_cap}  ntn=delay {d} {j} loss {l}")
            last_condition = condition

        for traffic_class in classes:
            df_row, valid = build_features(
                cat_cols, num_cols, lookup,
                traffic_class, terr_cond, ntn_cond,
            )
            if not valid:
                print(f"  {traffic_class:<15} [no metrics for this combination]")
                continue

            decision = decide(pipe, df_row)

            # Only touch tc when the decision actually changes. Rewriting
            # the filter every second means deleting it and re-adding it
            # every second, and in the window between the two this class
            # has no filter at all -- its packets fall through to the htb
            # default (1:99, unshaped) instead of the path it was assigned.
            if last_decision.get(traffic_class) == decision:
                print(f"  {traffic_class:<15} -> {decision:<12} [unchanged]")
                continue

            ok = update_tc_filter(DSCP[traffic_class], decision)
            status = "OK" if ok else "FAIL"
            if ok:
                last_decision[traffic_class] = decision
            else:
                last_decision.pop(traffic_class, None)
            print(f"  {traffic_class:<15} -> {decision:<12} [{status}]")

        print()
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nController stopped.")
