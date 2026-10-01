#!/usr/bin/env python3
"""
Test client for the A2 steering daemon -- stands in for the xApp until it
exists, and doubles as the stress-test driver for section 4.10.

Usage:
  steer_client.py send <class> <path>     one decision
  steer_client.py replay <terr> <ntn>     all four classes for a condition,
                                          decided by the A1 model
  steer_client.py status                  read the daemon's status file
"""
import importlib.util
import json
import socket
import sys
import time

SOCK_PATH = "/tmp/ntn_steer.sock"
STATUS_PATH = "/tmp/ntn_steer_status.json"
A1_CONTROLLER = "/home/bluelabuser/architecture_1/deploy_controller.py"

_seq = [0]


def send(cls, path, confidence=1.0, policy="test"):
    _seq[0] += 1
    msg = {"seq": _seq[0], "ts": time.time(), "class": cls,
           "path": path, "confidence": confidence, "policy": policy}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.sendto(json.dumps(msg).encode(), SOCK_PATH)
    s.close()
    return msg


def load_a1():
    spec = importlib.util.spec_from_file_location("a1_ctrl", A1_CONTROLLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]

    if cmd == "send":
        m = send(sys.argv[2], sys.argv[3])
        print("sent:", m)

    elif cmd == "replay":
        terr, ntn = sys.argv[2], sys.argv[3]
        a1 = load_a1()
        pipe, cat, num, lookup = a1.load_artifacts()
        for cls in a1.DSCP:
            row, valid = a1.build_features(cat, num, lookup, cls, terr, ntn)
            if not valid:
                print(f"  {cls:<15} [no metrics]")
                continue
            path = a1.decide(pipe, row)
            send(cls, path, policy="a1-tree-v1")
            print(f"  {cls:<15} -> {path}")

    elif cmd == "status":
        with open(STATUS_PATH) as f:
            print(json.dumps(json.load(f), indent=2))

    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
