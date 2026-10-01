#!/usr/bin/env python3
"""
Concurrent traffic mix -- JBM7 Deliverable 4.

All four traffic classes generated SIMULTANEOUSLY under a constrained
link, rather than one at a time with a gap between as every other
collection in this project does. Prioritisation only has meaning when
classes contend for the same capacity; measuring them sequentially never
creates that contention.

TESTING, NOT COLLECTION
-----------------------
The controller is deployed exactly as trained. Nothing here is re-labelled
or re-trained. Re-deriving labels under contention would change the
oracle's definition mid-project, and the more interesting claim is the
one this makes as-is: a controller trained on single-class measurements
still steers correctly under a traffic pattern it has never seen.

WHY THIS CANNOT BE DERIVED
--------------------------
The QoS outcome comparison in RESULTS.md 6.6 is derived by composing each
scheme's routing decisions with the per-path metrics in
master_metrics.csv, because both the live and derived methods measure
steady state on the chosen path. That shortcut does NOT apply here: those
metrics were measured one class at a time, and under contention the
per-class outcomes are different by definition. This one needs live
measurement.

IMPORTANT LIMITATION -- THE NTN PATH IS NOT BANDWIDTH-LIMITED
-------------------------------------------------------------
setup_steering.sh fixes the NTN HTB class at 1 Gbit and apply_conditions()
only ever changes its netem (delay/jitter/loss); the rate cap is applied
solely to the terrestrial class. NTN is therefore modelled as high-latency
and lossy but effectively infinite-capacity.

Two consequences for reading these results:

  1. Contention can only occur on the terrestrial path. Any scheme that
     offloads the large flows relieves it; NTN absorbs whatever it is
     given.
  2. In exactly the conditions where terrestrial is constrained, the
     learned controller and the fixed rule route the large flows the SAME
     way (both put Video and Background on NTN). They differ only at
     Moderate and above, where neither path is contended.

So the expected result is that tn-only collapses under contention while
every offloading scheme survives, with ML and the fixed rule close to
indistinguishable. That demonstrates the value of offloading, not the
value of *learned* offloading -- the latter is established by the QoS
outcome comparison in RESULTS.md 6.6 and the accuracy results, not here.

This also matters beyond this script: the JBM7 brief describes NTN
backhaul as "bandwidth-constrained", and this emulation does not
constrain it. Every result in the project characterises NTN as a latency
and loss trade-off, never a capacity one. Worth stating as a limitation.

THE REGIME THAT MATTERS
-----------------------
Total offered load across the four classes is 1650 kbps:

    terrestrial cap     vs 1650 kbps     regime
    Clear (uncapped)    --               no contention (control)
    Heavy   2 Mbit      83%              mild contention, queueing
    Stressed1 1 Mbit    165%             real contention  <-- the key case
    Stressed2 500 kbit  330%             severe
    Stressed3 300 kbit  550%             everything fails regardless

Stressed1 is where prioritisation has stakes: terrestrial can carry
roughly one class's worth, NTN is available, and the controller must
decide who goes where. Stressed3 is uninformative -- nothing survives on
either path, so every scheme scores the same.

Prerequisites: gNB, UE, steering plane, steer_daemon. NOT the RIC or the
xApp (this script pushes decisions itself), and deploy_controller.py MUST
be stopped or the two will fight over the tc filters.

Run as your own user:  python3 ~/architecture_2/concurrent_mix.py
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
OUT_CSV = "/home/bluelabuser/architecture_2/concurrent_mix_results.csv"
TUN = "oaitun_ue1"
CORE_IP = "10.45.0.1"

QOS = {
    "Voice-EF":      {"port": 5201, "pdb_ms": 100, "per": 1e-2, "rate_kbps": 100},
    "Video-AF41":    {"port": 5202, "pdb_ms": 150, "per": 1e-3, "rate_kbps": 1000},
    "Haptic-EF":     {"port": 5203, "pdb_ms": 5,   "per": 1e-3, "rate_kbps": 50},
    "Background-BE": {"port": 5204, "pdb_ms": 300, "per": 1e-6, "rate_kbps": 500},
}
OFFERED_TOTAL = sum(q["rate_kbps"] for q in QOS.values())   # 1650 kbps

FIXED_RULE = {"Voice-EF": "Terrestrial", "Haptic-EF": "Terrestrial",
              "Video-AF41": "NTN", "Background-BE": "NTN"}

# Chosen for the contention regime, not to cover the matrix. Clear is the
# control (no contention, every scheme should look alike); Stressed1 is
# the case the deliverable is really asking about.
# A utilisation gradient for tn-only (which puts all 1650 kbps on the
# terrestrial path), plus one case where the offload target is itself
# degraded.
CONDITIONS = [("Clear",     "Unimpaired"),   # control  -- uncapped, no contention
              ("Heavy",     "Unimpaired"),   #  83% of a 2 Mbit cap
              ("Stressed1", "Unimpaired"),   # 165% of a 1 Mbit cap
              ("Stressed2", "Unimpaired"),   # 330% of a 500 kbit cap
              ("Stressed1", "Sev-loss")]     # contention AND a 3%-loss offload target
SCHEMES = ["tn-only", "ntn-only", "fixed-rule", "ml"]
REPS = 2
DURATION = 30
UE_WAIT = 420

_seq = [0]


def load_a1():
    spec = importlib.util.spec_from_file_location("a1_ctrl", A1_CONTROLLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def push(cls, path):
    _seq[0] += 1
    msg = {"seq": _seq[0], "ts": time.time(), "class": cls, "path": path,
           "confidence": 1.0, "policy": "concurrent-mix"}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), SOCK_PATH)
    except OSError as e:
        print(f"  !! steering daemon unreachable: {e}", flush=True)
        raise
    finally:
        s.close()


def ue_up():
    return subprocess.run(
        f"ip -4 addr show {TUN} >/dev/null 2>&1 && "
        f"ping -I {TUN} -c1 -W2 {CORE_IP} >/dev/null 2>&1",
        shell=True).returncode == 0


def wait_for_ue():
    """Wait for ue_supervisor.sh to bring the UE back. Returns False if it
    never does. Exiting on the first drop would make the run need manual
    relaunching every ~6 minutes."""
    if ue_up():
        return True
    print(f"  .. UE down -- waiting up to {UE_WAIT}s", flush=True)
    waited = 0
    while waited < UE_WAIT and not ue_up():
        time.sleep(10)
        waited += 10
    if ue_up():
        print(f"  .. UE back after ~{waited}s", flush=True)
        time.sleep(3)
        return True
    return False


def apply_scheme(a1, pipe, cat, num, lookup, scheme, terr, ntn):
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


def measure_concurrent():
    """Run all four classes at once and return per-class results.

    This is the whole point of the script: four iperf3 clients started
    together, contending for the same constrained link, rather than one
    at a time. A single ping stream runs alongside for RTT -- it is a
    property of the path, not of a class, and four simultaneous ping
    streams would themselves add load.
    """
    ping = subprocess.Popen(
        ["ping", "-I", TUN, "-i", "0.2", "-c", str(DURATION * 5),
         "-W", "2", CORE_IP], stdout=subprocess.PIPE, text=True)

    procs = {}
    for cls, q in QOS.items():
        procs[cls] = subprocess.Popen(
            ["iperf3", "-c", CORE_IP, "-u", "-b", f"{q['rate_kbps']}K",
             "-l", "100", "-t", str(DURATION), "-p", str(q["port"]), "-J"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out = {}
    for cls, p in procs.items():
        # Unlike compare_arch.py, a timeout here is caught rather than
        # allowed to escape: one slow client must not kill the whole run.
        try:
            so, _ = p.communicate(timeout=DURATION + 25)
        except subprocess.TimeoutExpired:
            p.kill()
            print(f"  !! {cls}: iperf3 timed out", flush=True)
            out[cls] = None
            continue
        try:
            s = json.loads(so)["end"]["sum"]
            lost = (s.get("lost_percent") or 0.0) / 100.0
            # iperf3's UDP end.sum.bits_per_second is the SENDER's rate, so
            # it equals the offered rate no matter how much arrives. Under
            # contention that hides the entire effect: tn-only at 165%
            # utilisation reported 1650 kbps "goodput" while actually losing
            # 70% of it. Delivered rate is what the QoS test needs.
            sent_kbps = (s.get("bits_per_second") or 0) / 1000.0
            out[cls] = {"jitter_ms": s.get("jitter_ms"),
                        "loss_frac": lost,
                        "sent_kbps": round(sent_kbps, 1),
                        "goodput_kbps": sent_kbps * (1.0 - lost)}
        except (ValueError, KeyError):
            print(f"  !! {cls}: no iperf3 summary", flush=True)
            out[cls] = None

    rtt = mdev = None
    try:
        pout, _ = ping.communicate(timeout=30)
        for line in pout.splitlines():
            if "rtt min/avg/max" in line:
                parts = line.split("=")[1].strip().split()[0].split("/")
                rtt, mdev = float(parts[1]), float(parts[3])
    except subprocess.TimeoutExpired:
        ping.kill()

    for cls in out:
        if out[cls] is not None:
            out[cls]["rtt_ms"] = rtt
            out[cls]["rtt_mdev_ms"] = mdev
    return out


def compliant(cls, m):
    q = QOS[cls]
    return int(m["rtt_ms"] is not None
               and m["rtt_ms"] <= q["pdb_ms"]
               and m["loss_frac"] <= q["per"]
               and m["goodput_kbps"] >= q["rate_kbps"] * 0.98)


def done_cells():
    if not os.path.exists(OUT_CSV):
        return set()
    with open(OUT_CSV) as f:
        return {(r["scheme"], r["condition"], r["class"], r["rep"])
                for r in csv.DictReader(f)}


def main():
    a1 = load_a1()
    pipe, cat, num, lookup = a1.load_artifacts()

    # PROBE the socket, do not merely check the file exists. steer_daemon
    # does not unlink /tmp/ntn_steer.sock on exit, so the file outlives a
    # dead daemon by days. os.path.exists() therefore passes while every
    # push() raises ConnectionRefusedError, and the run dies after writing
    # only the CSV header.
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        probe.sendto(b'{"probe":1}', SOCK_PATH)
    except OSError as e:
        print(f"steer_daemon is not reachable on {SOCK_PATH}: "
              f"{type(e).__name__}: {e}")
        print("The socket file can exist while the daemon is dead. Start it:")
        print("  sudo -E python3 ~/architecture_2/steer_daemon.py")
        return 1
    finally:
        probe.close()

    done = done_cells()
    new = not os.path.exists(OUT_CSV)
    out = open(OUT_CSV, "a", newline="")
    w = csv.writer(out)
    if new:
        w.writerow(["scheme", "condition", "class", "rep", "path",
                    "rtt_ms", "rtt_mdev_ms", "jitter_ms", "loss_frac",
                    "goodput_kbps", "compliant", "offered_total_kbps",
                    "terr_cap_kbps", "sent_kbps"])

    caps = {"Clear": None, "Moderate": 4000, "Heavy": 2000,
            "Stressed1": 1000, "Stressed2": 500, "Stressed3": 300}

    total = len(SCHEMES) * len(CONDITIONS) * REPS
    n = 0
    print(f"Concurrent mix: all {len(QOS)} classes together, "
          f"{OFFERED_TOTAL} kbps offered")
    print(f"{total} runs ({len(SCHEMES)} schemes x {len(CONDITIONS)} "
          f"conditions x {REPS} reps), {DURATION}s each\n")

    for terr, ntn in CONDITIONS:
        cond = f"{terr}:{ntn}"
        cap = caps.get(terr)
        util = "n/a" if cap is None else f"{100*OFFERED_TOTAL/cap:.0f}%"
        print(f"=== {cond}  (terrestrial cap {cap or 'uncapped'} kbps, "
              f"offered {OFFERED_TOTAL} kbps = {util}) ===", flush=True)
        for scheme in SCHEMES:
            for rep in range(1, REPS + 1):
                if all((scheme, cond, c, str(rep)) in done for c in QOS):
                    n += 1
                    continue
                if not wait_for_ue():
                    print(f"  !! UE did not recover -- stopping. Re-run to "
                          f"resume; completed runs are skipped.", flush=True)
                    out.close()
                    return 2

                with open(COND_FILE, "w") as f:
                    f.write(cond + "\n")
                time.sleep(2)
                policy = apply_scheme(a1, pipe, cat, num, lookup,
                                      scheme, terr, ntn)
                n += 1
                res = measure_concurrent()

                line = []
                for cls in QOS:
                    m = res.get(cls)
                    if m is None:
                        line.append(f"{cls}=FAIL")
                        continue
                    c = compliant(cls, m)
                    w.writerow([scheme, cond, cls, rep, policy[cls],
                                m["rtt_ms"], m["rtt_mdev_ms"],
                                m["jitter_ms"], f"{m['loss_frac']:.6f}",
                                round(m["goodput_kbps"], 1), c,
                                OFFERED_TOTAL, cap or "", m["sent_kbps"]])
                    line.append(f"{cls.split('-')[0]}={'OK' if c else 'VIOL'}"
                                f"/{m['goodput_kbps']:.0f}k")
                out.flush()
                print(f"  [{n}/{total}] {scheme:<11} r{rep}  " + "  ".join(line),
                      flush=True)

    out.close()
    print("\nWrote", OUT_CSV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
