#!/usr/bin/env python3
"""
Architecture 2 ML xApp.

Runs A1's trained model inside the FlexRIC Near-RT RIC control loop and
pushes path decisions to the A2 steering daemon. The model is loaded from
A1's model.pkl unchanged -- not re-exported, not re-fitted -- so A1 and A2
run byte-identical weights and any measured difference is attributable to
controller placement.

Cadence: E2 indications arrive every 10 ms (the fastest FlexRIC interval),
but A1's controller decides once per second. Deciding on every indication
would make A2 a 100x faster controller and the comparison would measure
cadence rather than placement, so inference is rate-limited to A1's
LOOP_INTERVAL. The indication still drives the loop, so the T1..T4 chain
is measured on the real E2 path.

Hysteresis/dwell (guide 4.8) is implemented but DISABLED by default: A1 has
none, and enabling it for A2 only would confound the comparison. Set
DWELL_SEC > 0 for a separate experiment.

Run:  ~/architecture_2/run_ml_xapp.sh
"""
import json
import os
import socket
import sys
import time

sys.path.insert(
    0, "/home/bluelabuser/openairinterface5g/openair2/E2AP/flexric/build/"
       "examples/xApp/python3")

import xapp_sdk as ric  # noqa: E402
import importlib.util  # noqa: E402

A1_CONTROLLER = "/home/bluelabuser/architecture_1/deploy_controller.py"
SOCK_PATH = "/tmp/ntn_steer.sock"
CONDITION_FILE = "/tmp/current_condition.txt"
TIMING_CSV = "/home/bluelabuser/architecture_2/xapp_timing.csv"

CONTROL_PERIOD = float(os.environ.get("CONTROL_PERIOD", "1.0"))
DWELL_SEC = float(os.environ.get("DWELL_SEC", "0"))
DURATION = float(os.environ.get("XAPP_DURATION", "60"))
E2_INTERVAL = os.environ.get("E2_INTERVAL", "10")
POLICY = "a1-tree-v1"


def load_a1():
    spec = importlib.util.spec_from_file_location("a1_ctrl", A1_CONTROLLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


a1 = load_a1()
PIPE, CAT, NUM, LOOKUP = a1.load_artifacts()
CLASSES = list(a1.DSCP.keys())

sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
timing = open(TIMING_CSV, "a", buffering=1)
if timing.tell() == 0:
    timing.write("seq,class,path,t1_ind_us,t2_infer_start,t3_infer_end,"
                 "t4_sent,infer_us,total_us,changed\n")

state = {
    "seq": 0,
    "last_decision": {},
    "last_change": {},
    "last_control": 0.0,
    "ran": {},
    "decisions": 0,
    "sent": 0,
}


def read_condition():
    try:
        with open(CONDITION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def path_state_mtime():
    """T0 -- when the path state this decision is based on last changed.

    The path metrics come from A1's static per-condition lookup, so the
    condition file's mtime is the moment the path state actually changed.
    A live telemetry sidecar would stamp its own sample time here instead.
    """
    try:
        return os.stat(CONDITION_FILE).st_mtime
    except OSError:
        return 0.0


def send_decision(cls, path, confidence):
    state["seq"] += 1
    msg = {"seq": state["seq"], "ts": time.time(), "class": cls,
           "path": path, "confidence": confidence, "policy": POLICY,
           "t0": path_state_mtime()}
    sock.sendto(json.dumps(msg).encode(), SOCK_PATH)
    state["sent"] += 1
    return msg


def control_step(t1_ind):
    """One decision pass. t1_ind is the E2 indication arrival time."""
    condition = read_condition()
    if not condition:
        return
    try:
        terr, ntn = condition.split(":", 1)
    except ValueError:
        return

    for cls in CLASSES:
        row, valid = a1.build_features(CAT, NUM, LOOKUP, cls, terr, ntn)
        if not valid:
            continue

        t2 = time.time()
        pred = PIPE.predict(row)[0]
        try:
            proba = PIPE.predict_proba(row)[0]
            confidence = float(max(proba))
        except (AttributeError, IndexError):
            confidence = 1.0
        t3 = time.time()

        path = "NTN" if int(pred) == 1 else "Terrestrial"
        state["decisions"] += 1

        prev = state["last_decision"].get(cls)
        changed = prev != path

        if changed and DWELL_SEC > 0:
            since = t3 - state["last_change"].get(cls, 0.0)
            if since < DWELL_SEC:
                changed = False
                path = prev

        if changed:
            send_decision(cls, path, confidence)
            state["last_decision"][cls] = path
            state["last_change"][cls] = t3
            t4 = time.time()
            print(f"  {cls:<15} -> {path:<12} conf={confidence:.2f} "
                  f"infer={(t3 - t2) * 1e6:.0f}us", flush=True)
        else:
            t4 = time.time()

        timing.write(f"{state['seq']},{cls},{path},{t1_ind:.6f},{t2:.6f},"
                     f"{t3:.6f},{t4:.6f},{(t3 - t2) * 1e6:.1f},"
                     f"{(t4 - t1_ind) * 1e6:.1f},{int(changed)}\n")


class MACCallback(ric.mac_cb):
    def __init__(self):
        ric.mac_cb.__init__(self)

    def handle(self, ind):
        t1 = time.time()
        if len(ind.ue_stats) > 0:
            state["ran"]["rnti"] = ind.ue_stats[0].rnti
            state["ran"]["tstamp"] = ind.tstamp
        if t1 - state["last_control"] < CONTROL_PERIOD:
            return
        state["last_control"] = t1
        control_step(t1)


def main():
    ric.init()
    conn = ric.conn_e2_nodes()
    assert len(conn) > 0, "No E2 nodes connected -- is the gNB up?"

    print("=" * 62)
    print("A2 ML xAPP")
    print(f"  E2 nodes       : {len(conn)} "
          f"(MCC={conn[0].id.plmn.mcc} MNC={conn[0].id.plmn.mnc})")
    print(f"  model          : {a1.MODEL_PATH}")
    print(f"  control period : {CONTROL_PERIOD}s (A1 parity)")
    print(f"  E2 interval    : {E2_INTERVAL}ms")
    print(f"  dwell          : {DWELL_SEC}s "
          f"{'(disabled - A1 parity)' if DWELL_SEC == 0 else ''}")
    print(f"  steering socket: {SOCK_PATH}")
    print("=" * 62, flush=True)

    interval = {"1": ric.Interval_ms_1, "2": ric.Interval_ms_2,
                "5": ric.Interval_ms_5, "10": ric.Interval_ms_10}[E2_INTERVAL]
    cb = MACCallback()
    hndlrs = [ric.report_mac_sm(conn[i].id, interval, cb)
              for i in range(len(conn))]

    t_end = time.time() + DURATION
    try:
        while time.time() < t_end:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    for h in hndlrs:
        ric.rm_report_mac_sm(h)
    ric.try_stop()

    print(f"\ndecisions={state['decisions']} sent={state['sent']} "
          f"timing -> {TIMING_CSV}")


if __name__ == "__main__":
    main()
