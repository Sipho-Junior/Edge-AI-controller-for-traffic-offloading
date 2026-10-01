#!/usr/bin/env python3
"""
Cross-architecture comparison -- guide section 6.

Produces the 6.2 core comparison table and the 6.3 decision-path latency
breakdown, plus the 6.4 minimum baselines (TN-only, NTN-only, static class
mapping, supervised ML).

Fairness (6.1): same OAI commit, same traffic generators and class
definitions, same condition set, same model weights, same control interval.
Every scheme is enforced through the identical tc plane -- A2's daemon
imports A1's update_tc_filter -- so the routing mechanism is held constant
and only the decision source varies.

Results are appended per cell so a UE drop mid-run loses one measurement
rather than the whole matrix; re-running skips cells already recorded.
"""
import csv
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time

A1_CONTROLLER = "/home/bluelabuser/architecture_1/deploy_controller.py"
SOCK_PATH = "/tmp/ntn_steer.sock"
COND_FILE = "/tmp/current_condition.txt"
# v2: written to a NEW file. comparison_results.csv holds 127 rows produced
# with the superseded pre-intermittency model (md5 9726d0e3) and the old
# labelling rule. done_cells() skips any (scheme, condition, class, rep)
# already present, so re-using that file would skip most of the matrix and
# silently interleave two models' results in one table.
OUT_CSV = "/home/bluelabuser/architecture_2/comparison_results_v2.csv"
TUN = "oaitun_ue1"
CORE_IP = "10.45.0.1"

# Table 1.4: 5QI-derived thresholds. rate_kbps is the offered load, which
# the third compliance term requires.
QOS = {
    "Voice-EF":      {"port": 5201, "pdb_ms": 100, "per": 1e-2, "rate_kbps": 100},
    "Video-AF41":    {"port": 5202, "pdb_ms": 150, "per": 1e-3, "rate_kbps": 1000},
    "Haptic-EF":     {"port": 5203, "pdb_ms": 5,   "per": 1e-3, "rate_kbps": 50},
    "Background-BE": {"port": 5204, "pdb_ms": 300, "per": 1e-6, "rate_kbps": 500},
}

FIXED_RULE = {"Voice-EF": "Terrestrial", "Haptic-EF": "Terrestrial",
              "Video-AF41": "NTN", "Background-BE": "NTN"}

# The first two have an unimpaired NTN path, so NTN is never the wrong
# choice and every scheme except TN-only scores identically. The second two
# are the diagnostic cases: Clear:Sev-loss makes NTN clearly worse (correct
# answer: stay terrestrial, which the fixed rule cannot do), and
# Stressed3:Worst-loss degrades both paths so the decision falls through to
# the least-non-compliant tiebreak.
# Six conditions, each chosen to demonstrate something distinct rather than
# to cover the matrix uniformly (the full 54 pairs would be ~7 hours).
#   Clear:Unimpaired         baseline -- every scheme should look similar
#   Stressed3:Unimpaired     controller OFFLOADS; fixed rule pins Voice on 300 kbit
#   Clear:Sev-loss           controller DECLINES to offload -- the source of the margin
#   Stressed3:Sev-loss       both paths degraded; per-class split
#   Clear:Intermittent-deep  intermittency: latency looks normal, only loss reveals it
#   Stressed3:Worst-loss     retained from the v1 run for continuity
CONDITIONS = [("Clear", "Unimpaired"), ("Stressed3", "Unimpaired"),
              ("Clear", "Sev-loss"), ("Stressed3", "Sev-loss"),
              ("Clear", "Intermittent-deep"), ("Stressed3", "Worst-loss")]
SCHEMES = ["tn-only", "ntn-only", "fixed-rule", "ml"]
REPS = 2
# Seconds to wait for the UE to be restarted by ue_supervisor.sh before
# giving up. A normal recovery takes 30-60 s.
UE_WAIT = 420
# 20 s, not 6. The intermittent profiles cycle every 20 s (15 s up, 5 s down),
# so a 6 s window either catches an outage or does not, essentially at random,
# and the measured loss for those cells would be noise rather than the duty
# cycle. One full cycle makes the outage fraction deterministic. The same
# aliasing was observed during collection: a 30 s window against the 20 s
# cycle produced 6.7% or 13.3% loss per run and nothing in between.
DURATION = 20

_seq = [0]


def load_a1():
    spec = importlib.util.spec_from_file_location("a1", A1_CONTROLLER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def push(cls, path):
    _seq[0] += 1
    msg = {"seq": _seq[0], "ts": time.time(), "class": cls, "path": path,
           "confidence": 1.0, "policy": "comparison", "t0": time.time()}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.sendto(json.dumps(msg).encode(), SOCK_PATH)
    s.close()


def ue_up():
    return subprocess.run(
        f"ip -4 addr show {TUN} >/dev/null 2>&1 && "
        f"ping -I {TUN} -c1 -W2 {CORE_IP} >/dev/null 2>&1",
        shell=True).returncode == 0


def apply_scheme(a1, pipe, cat, num, lookup, scheme, terr, ntn):
    """Set the routing policy for this scheme and return it."""
    policy = {}
    for cls in QOS:
        if scheme == "tn-only":
            policy[cls] = "Terrestrial"
        elif scheme == "ntn-only":
            policy[cls] = "NTN"
        elif scheme == "fixed-rule":
            policy[cls] = FIXED_RULE[cls]
        else:
            row, valid = a1.build_features(cat, num, lookup, cls, terr, ntn)
            policy[cls] = a1.decide(pipe, row) if valid else "Terrestrial"
    for cls, path in policy.items():
        push(cls, path)
    time.sleep(2.5)
    return policy


def measure(cls):
    """Per-class RTT, jitter, loss, goodput for one run."""
    q = QOS[cls]
    ping = subprocess.Popen(
        ["ping", "-I", TUN, "-i", "0.2", "-c", str(DURATION * 5),
         "-W", "2", CORE_IP], stdout=subprocess.PIPE, text=True)
    r = subprocess.run(
        ["iperf3", "-c", CORE_IP, "-u", "-b", f"{q['rate_kbps']}K",
         "-l", "100", "-t", str(DURATION), "-p", str(q["port"]), "-J"],
        capture_output=True, text=True, timeout=DURATION + 20)
    pout, _ = ping.communicate(timeout=30)

    rtt = jitter_ping = None
    for line in pout.splitlines():
        if "rtt min/avg/max" in line:
            parts = line.split("=")[1].strip().split()[0].split("/")
            rtt, jitter_ping = float(parts[1]), float(parts[3])

    try:
        js = json.loads(r.stdout)
        s = js["end"]["sum"]
        return {"rtt_ms": rtt, "rtt_mdev_ms": jitter_ping,
                "jitter_ms": s.get("jitter_ms"),
                "loss_frac": (s.get("lost_percent") or 0.0) / 100.0,
                "goodput_kbps": (s.get("bits_per_second") or 0) / 1000.0}
    except (ValueError, KeyError):
        return None


def compliant(cls, m):
    q = QOS[cls]
    if m["rtt_ms"] is None:
        return None
    return int(m["rtt_ms"] <= q["pdb_ms"]
               and m["loss_frac"] <= q["per"]
               and m["goodput_kbps"] >= q["rate_kbps"] * 0.95)


def done_cells():
    if not os.path.exists(OUT_CSV):
        return set()
    with open(OUT_CSV) as f:
        return {(r["scheme"], r["condition"], r["class"], r["rep"])
                for r in csv.DictReader(f)}


def main():
    a1 = load_a1()
    pipe, cat, num, lookup = a1.load_artifacts()
    done = done_cells()

    new = not os.path.exists(OUT_CSV)
    out = open(OUT_CSV, "a", newline="")
    w = csv.writer(out)
    if new:
        w.writerow(["scheme", "condition", "class", "rep", "path",
                    "rtt_ms", "rtt_mdev_ms", "jitter_ms", "loss_frac",
                    "goodput_kbps", "compliant"])

    total = len(SCHEMES) * len(CONDITIONS) * len(QOS) * REPS
    n = 0
    for terr, ntn in CONDITIONS:
        cond = f"{terr}:{ntn}"
        for scheme in SCHEMES:
            for rep in range(1, REPS + 1):
                pending = [c for c in QOS
                           if (scheme, cond, c, str(rep)) not in done]
                if not pending:
                    n += len(QOS)
                    continue

                if not ue_up():
                    # WAIT for the UE rather than exiting. The NTN RFSim UE
                    # loses sync roughly every 6 minutes by design, so over a
                    # ~96 minute matrix it will drop ~15 times. Exiting on the
                    # first drop means the run has to be relaunched by hand on
                    # every one of them, which makes the matrix effectively
                    # uncompletable. ue_supervisor.sh restarts the UE within
                    # ~30-60 s, so waiting costs a minute and saves the run.
                    print(f"  .. UE down at {scheme}/{cond}/rep{rep} -- "
                          f"waiting up to {UE_WAIT}s for it to recover",
                          flush=True)
                    waited = 0
                    while waited < UE_WAIT and not ue_up():
                        time.sleep(10)
                        waited += 10
                    if not ue_up():
                        print(f"  !! UE did not recover in {UE_WAIT}s -- "
                              f"stopping. Re-run to resume; completed cells "
                              f"are skipped.", flush=True)
                        out.close()
                        return 2
                    print(f"  .. UE back after ~{waited}s, continuing",
                          flush=True)
                    # Re-apply the scheme: a UE restart tears down the PDU
                    # session, and the condition/policy must be re-asserted
                    # before the next measurement is trustworthy.
                    time.sleep(3)

                with open(COND_FILE, "w") as f:
                    f.write(cond + "\n")
                time.sleep(2)
                policy = apply_scheme(a1, pipe, cat, num, lookup,
                                      scheme, terr, ntn)

                for cls in pending:
                    n += 1
                    m = measure(cls)
                    if m is None:
                        print(f"  [{n}/{total}] {scheme}/{cond}/{cls}/"
                              f"r{rep}: measurement failed", flush=True)
                        continue
                    c = compliant(cls, m)
                    w.writerow([scheme, cond, cls, rep, policy[cls],
                                m["rtt_ms"], m["rtt_mdev_ms"],
                                m["jitter_ms"], f"{m['loss_frac']:.6f}",
                                f"{m['goodput_kbps']:.1f}", c])
                    out.flush()
                    print(f"  [{n}/{total}] {scheme:<11} {cond:<22} "
                          f"{cls:<15} r{rep} {policy[cls]:<12} "
                          f"rtt={m['rtt_ms']}ms loss={m['loss_frac']:.4f} "
                          f"gp={m['goodput_kbps']:.0f}k "
                          f"{'OK' if c else 'VIOL'}", flush=True)

    out.close()
    print("\nWrote", OUT_CSV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
