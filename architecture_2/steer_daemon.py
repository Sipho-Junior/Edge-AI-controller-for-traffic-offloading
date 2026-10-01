#!/usr/bin/env python3
"""
Architecture 2 steering daemon.

A1 computed the decision and enforced it in one process. A2 splits those:
the xApp decides, this daemon enforces. To keep the comparison honest the
enforcement code is not reimplemented here -- it is imported from A1's
deploy_controller.py, so both architectures drive tc through byte-identical
logic and any measured difference is attributable to controller placement
rather than to a divergent forwarding plane.

Link emulation (NetEm profile, terrestrial rate cap) also stays here. It is
testbed scaffolding, not a control decision, so it must behave identically
under both architectures.

Protocol: one JSON object per datagram on a Unix SOCK_DGRAM socket.
    {"seq": 42, "ts": 1789655754.692, "class": "Video-AF41",
     "path": "NTN", "confidence": 0.93, "policy": "a1-tree-v1"}

Run as root:  sudo -E python3 ~/architecture_2/steer_daemon.py
"""
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

A1_CONTROLLER = "/home/bluelabuser/architecture_1/deploy_controller.py"
SOCK_PATH = "/tmp/ntn_steer.sock"
STATUS_PATH = "/tmp/ntn_steer_status.json"
TIMING_CSV = "/home/bluelabuser/architecture_2/steer_timing.csv"
SWITCH_CSV = "/home/bluelabuser/architecture_2/switch_latency.csv"
CONDITION_FILE = "/tmp/current_condition.txt"

# T6 is found by polling the target HTB class until its packet counter
# moves. Each poll is a `tc` subprocess, so resolution is bounded at a few
# milliseconds -- fine against a ~16 ms enforcement cost, but it means T6
# is an upper bound, not an exact arrival time.
T6_POLL = 0.002
T6_TIMEOUT = 10.0

# No decision for this long means the xApp or RIC is gone. The filters are
# left exactly as they are -- tc state persists, so "last known good" is
# the default behaviour and tearing it down would be strictly worse.
STALE_AFTER = 5.0
CONDITION_POLL = 1.0

VALID_PATHS = ("NTN", "Terrestrial")


def load_a1():
    spec = importlib.util.spec_from_file_location("a1_ctrl", A1_CONTROLLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def class_packets(iface, flowid):
    out = subprocess.run(["tc", "-s", "class", "show", "dev", iface],
                         capture_output=True, text=True).stdout
    cur = None
    for line in out.splitlines():
        m = re.search(r"class htb (1:\w+)", line)
        if m:
            cur = m.group(1)
        m = re.search(r"Sent \d+ bytes (\d+) pkt", line)
        if m and cur == flowid:
            return int(m.group(1))
    return None


def watch_first_packet(switch_log, iface, flowid, baseline, cls, path,
                       seq, t0, t4, t5):
    """T6: block until a packet actually lands on the newly selected path."""
    deadline = time.time() + T6_TIMEOUT
    while time.time() < deadline:
        n = class_packets(iface, flowid)
        if n is not None and baseline is not None and n > baseline:
            t6 = time.time()
            switch_log.write(
                f"{seq},{cls},{path},{flowid},{t0:.6f},{t4:.6f},{t5:.6f},"
                f"{t6:.6f},{(t5 - t4) * 1e6:.1f},{(t6 - t5) * 1e6:.1f},"
                f"{(t6 - t0) * 1e6:.1f}\n")
            print(f"[switch] {cls:<15} first packet on {flowid} after "
                  f"{(t6 - t5) * 1e3:.1f} ms (observable {(t6 - t0) * 1e3:.1f} ms)",
                  flush=True)
            return
        time.sleep(T6_POLL)
    switch_log.write(f"{seq},{cls},{path},{flowid},{t0:.6f},{t4:.6f},"
                     f"{t5:.6f},,{(t5 - t4) * 1e6:.1f},,\n")
    print(f"[switch] {cls:<15} no packet on {flowid} within {T6_TIMEOUT}s "
          f"(class idle)", flush=True)


def write_status(state):
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATUS_PATH)


def main():
    a1 = load_a1()

    if not a1.ensure_qdisc_setup():
        print("Steering plane is not set up. Run architecture_1/setup_steering.sh")
        sys.exit(1)

    classes = list(a1.DSCP.keys())

    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    sock.settimeout(CONDITION_POLL)

    timing = open(TIMING_CSV, "a", buffering=1)
    if timing.tell() == 0:
        timing.write("seq,class,path,t4_sent,t5_applied,enforce_us,changed\n")

    switch_log = open(SWITCH_CSV, "a", buffering=1)
    if switch_log.tell() == 0:
        switch_log.write("seq,class,path,flowid,t0,t4,t5,t6,"
                         "enforce_us,to_first_packet_us,observable_us\n")

    applied = {}
    last_condition = None
    last_msg_time = 0.0
    stale_logged = False
    n_msgs = n_changes = n_bad = 0

    print("=" * 62)
    print("A2 STEERING DAEMON")
    print(f"  socket    : {SOCK_PATH}")
    print(f"  enforcing : {A1_CONTROLLER}")
    print(f"  interface : {a1.IFACE}")
    print(f"  timing    : {TIMING_CSV}")
    print("=" * 62, flush=True)

    while True:
        # Link emulation tracks the condition file exactly as A1 does.
        try:
            with open(CONDITION_FILE) as f:
                condition = f.read().strip()
        except FileNotFoundError:
            condition = None

        if condition and condition != last_condition:
            try:
                terr, ntn = condition.split(":", 1)
            except ValueError:
                print(f"[cond] malformed: {condition!r}")
                terr = ntn = None
            if terr and ntn:
                if a1.apply_conditions(terr, ntn):
                    cap = a1.TERR_PROFILES[terr][0] or "uncapped"
                    d, j, l = a1.NTN_PROFILES[ntn]
                    print(f"[cond] {condition}  terr={cap}  "
                          f"ntn=delay {d} {j} loss {l}", flush=True)
                last_condition = condition

        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            if last_msg_time and not stale_logged and \
                    time.time() - last_msg_time > STALE_AFTER:
                print(f"[stale] no decision for {STALE_AFTER}s -- holding "
                      f"last known policy: {applied}", flush=True)
                stale_logged = True
                write_status({"state": "stale", "applied": applied,
                              "last_msg": last_msg_time})
            continue

        t_recv = time.time()
        try:
            msg = json.loads(data.decode())
            cls = msg["class"]
            path = msg["path"]
        except (ValueError, KeyError) as e:
            n_bad += 1
            print(f"[bad] {e}: {data[:120]!r}", flush=True)
            continue

        if cls not in a1.DSCP or path not in VALID_PATHS:
            n_bad += 1
            print(f"[bad] unknown class/path: {cls!r} {path!r}", flush=True)
            continue

        n_msgs += 1
        last_msg_time = t_recv
        if stale_logged:
            print("[stale] decisions resumed", flush=True)
            stale_logged = False

        changed = applied.get(cls) != path
        if changed:
            flowid = a1.CLASS_NTN if path == "NTN" else a1.CLASS_TERRESTRIAL
            baseline = class_packets(a1.IFACE, flowid)

            ok = a1.update_tc_filter(a1.DSCP[cls], path)
            t_applied = time.time()
            if ok:
                applied[cls] = path
                n_changes += 1
                print(f"[apply] {cls:<15} -> {path:<12} "
                      f"({(t_applied - t_recv) * 1e6:.0f} us)", flush=True)
                threading.Thread(
                    target=watch_first_packet,
                    args=(switch_log, a1.IFACE, flowid, baseline, cls, path,
                          msg.get("seq", ""), float(msg.get("t0", 0.0)),
                          float(msg.get("ts", t_recv)), t_applied),
                    daemon=True).start()
            else:
                print(f"[apply] {cls:<15} -> {path:<12} FAILED", flush=True)
        else:
            t_applied = time.time()

        timing.write(f"{msg.get('seq','')},{cls},{path},"
                     f"{msg.get('ts','')},{t_applied:.6f},"
                     f"{(t_applied - t_recv) * 1e6:.1f},{int(changed)}\n")

        write_status({"state": "ok", "applied": applied,
                      "msgs": n_msgs, "changes": n_changes,
                      "bad": n_bad, "last_msg": last_msg_time,
                      "condition": last_condition})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDaemon stopped. tc filters left in their last state.")
