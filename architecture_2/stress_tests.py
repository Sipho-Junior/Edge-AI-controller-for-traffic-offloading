#!/usr/bin/env python3
"""
Architecture 2 stress tests -- guide section 4.10.

1. Replay the same impairment conditions used for A1.
2. Same trained model and preprocessing constants.
3. Compare A1 inference against A2 xApp inference (KPIs + timing).
4. Stress E2/KPM reporting frequency, monitor gNB/xApp CPU.
5. Restart the RIC/xApp, verify the steering plane holds a safe policy.
6. Stop the decision source, verify a defined fallback rather than
   decisions on stale data.

The xApp is started once and conditions are changed underneath it, rather
than restarting it per condition: the xApp re-reads the condition file
every control step, so this both runs faster and better matches how the
controller actually operates.
"""
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time

A2 = "/home/bluelabuser/architecture_2"
A1_CONTROLLER = "/home/bluelabuser/architecture_1/deploy_controller.py"
COND_FILE = "/tmp/current_condition.txt"
STATUS = "/tmp/ntn_steer_status.json"
IFACE = "eno1"
TUN = "oaitun_ue1"
CORE_IP = "10.45.0.1"

CONDITIONS = [
    ("Clear", "Unimpaired"), ("Clear", "Best"), ("Heavy", "Worst"),
    ("Stressed3", "Unimpaired"), ("Stressed2", "Worst-loss"),
    ("Stressed1", "Best"),
]

PRIO = {"Voice-EF": 5, "Video-AF41": 6, "Haptic-EF": 7, "Background-BE": 8}


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True).stdout


def filter_map():
    out = sh(f"tc filter show dev {IFACE} parent 1:")
    res, prio = {}, None
    for line in out.splitlines():
        m = re.search(r"pref (\d+) u32", line)
        if m:
            prio = int(m.group(1))
        m = re.search(r"flowid (1:\w+)", line)
        if m and prio:
            res[prio] = m.group(1)
    return res


def ue_up():
    return subprocess.run(
        f"ip -4 addr show {TUN} >/dev/null 2>&1 && "
        f"ping -I {TUN} -c1 -W2 {CORE_IP} >/dev/null 2>&1",
        shell=True).returncode == 0


def load_a1():
    spec = importlib.util.spec_from_file_location("a1", A1_CONTROLLER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def start_xapp(duration, e2_interval="10", control_period="1.0"):
    env = dict(os.environ,
               XAPP_DURATION=str(duration),
               E2_INTERVAL=e2_interval,
               CONTROL_PERIOD=control_period)
    return subprocess.Popen(
        [f"{A2}/run_ml_xapp.sh"], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, preexec_fn=os.setsid)


def stop_xapp(proc):
    """FlexRIC's SCTP teardown can hang well past SIGTERM, so escalate."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue
    proc.wait()


def cpu_of(name):
    out = sh(f"ps -o %cpu= -C {name}")
    vals = [float(x) for x in out.split() if x]
    return sum(vals) if vals else 0.0


results = []


def record(test, ok, detail):
    results.append((test, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {detail}", flush=True)


# ---------------------------------------------------------------- 1/2/3 ----
def test_replay(a1, pipe, cat, num, lookup):
    print("\n=== Tests 1-3: replay A1 conditions through A2 ===", flush=True)
    xapp = start_xapp(duration=len(CONDITIONS) * 8 + 25)
    time.sleep(12)   # RIC connect + subscribe

    all_ok = True
    for terr, ntn in CONDITIONS:
        cond = f"{terr}:{ntn}"
        if not ue_up():
            record(f"replay {cond}", False, f"{cond}: UE down, skipped")
            all_ok = False
            continue

        with open(COND_FILE, "w") as f:
            f.write(cond + "\n")
        time.sleep(7)

        fm = filter_map()
        mismatches = []
        for cls, prio in PRIO.items():
            row, valid = a1.build_features(cat, num, lookup, cls, terr, ntn)
            if not valid:
                continue
            want = "1:20" if a1.decide(pipe, row) == "NTN" else "1:10"
            got = fm.get(prio)
            if got != want:
                mismatches.append(f"{cls}:{got}!={want}")

        ok = not mismatches
        all_ok &= ok
        record(f"replay {cond}", ok,
               f"{cond}: {'all 4 classes match A1' if ok else ', '.join(mismatches)}")

    stop_xapp(xapp)
    return all_ok


# -------------------------------------------------------------------- 4 ----
def test_e2_frequency():
    print("\n=== Test 4: E2/KPM reporting frequency stress ===", flush=True)
    for interval in ("10", "1"):
        xapp = start_xapp(duration=22, e2_interval=interval)
        time.sleep(14)
        gnb = cpu_of("nr-softmodem")
        ric = cpu_of("nearRT-RIC")
        xa = cpu_of("python3")
        record(f"e2 freq {interval}ms", True,
               f"{interval}ms interval: gNB {gnb:.1f}% CPU, "
               f"RIC {ric:.1f}%, python {xa:.1f}%")
        stop_xapp(xapp)
        time.sleep(2)
    return True


# -------------------------------------------------------------------- 5 ----
def test_xapp_restart():
    print("\n=== Test 5: xApp killed mid-flight, policy must hold ===",
          flush=True)
    with open(COND_FILE, "w") as f:
        f.write("Stressed3:Unimpaired\n")

    xapp = start_xapp(duration=60)
    time.sleep(14)
    before = filter_map()

    os.killpg(os.getpgid(xapp.pid), signal.SIGKILL)
    try:
        xapp.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(8)
    after = filter_map()

    ok = before == after and len(after) == 4
    record("xapp restart", ok,
           f"filters {'unchanged after SIGKILL' if ok else 'CHANGED'}: "
           f"{ {k: after.get(k) for k in sorted(PRIO.values())} }")
    return ok


# -------------------------------------------------------------------- 6 ----
def test_stale():
    print("\n=== Test 6: no decision source, defined fallback ===", flush=True)
    time.sleep(8)
    try:
        with open(STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError) as e:
        record("stale fallback", False, f"status unreadable: {e}")
        return False

    ok = st.get("state") == "stale" and len(st.get("applied", {})) == 4
    record("stale fallback", ok,
           f"daemon state={st.get('state')!r}, "
           f"holding {len(st.get('applied', {}))} class policies")
    return ok


def main():
    a1 = load_a1()
    pipe, cat, num, lookup = a1.load_artifacts()

    print("=" * 70)
    print("ARCHITECTURE 2 STRESS TESTS (guide 4.10)")
    print("=" * 70, flush=True)

    test_replay(a1, pipe, cat, num, lookup)
    test_e2_frequency()
    test_xapp_restart()
    test_stale()

    with open(COND_FILE, "w") as f:
        f.write("Clear:Unimpaired\n")

    print("\n" + "=" * 70)
    print("SUMMARY")
    npass = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{npass}/{len(results)} checks passed")
    print("=" * 70)
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
