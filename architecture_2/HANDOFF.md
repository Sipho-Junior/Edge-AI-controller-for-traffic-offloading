# Architecture 2 — handoff

State of A2 as of 2026-09-18, written so a session with no prior context can
pick it up. Build instructions are in `BUILD_NOTES.md`; this file covers
design rationale, results and caveats.

## Provenance

| Item | Value |
|---|---|
| OAI commit | `b3930e39f5` (branch `develop`) |
| FlexRIC commit | `ef6d722f` (submodule, duranta-project fork) |
| Model | `~/architecture_1/training_data/model.pkl`, md5 `9726d0e3540eaa716728270dcc59be34` |
| Model type | sklearn Pipeline: OneHotEncoder + DecisionTree, `max_depth=4` |
| E2AP / KPM | `E2AP_V2` / `KPM_V2_03` (defaults on both sides — they already match) |

## What A2 is

A1 computed the routing decision and enforced it in one Python process.
A2 splits those in two:

```
E2 indication ──> ml_xapp.py (FlexRIC xApp, decides)
                       │ JSON datagram
                       v  /tmp/ntn_steer.sock
                  steer_daemon.py (root, enforces)
                       │
                       v  tc filter -> HTB class 1:10 (TN) / 1:20 (NTN)
```

**Design rule that makes the comparison valid:** `steer_daemon.py` does not
reimplement enforcement. It imports `~/architecture_1/deploy_controller.py`
and calls A1's own `update_tc_filter`, `apply_conditions`, `DSCP`,
`NTN_PROFILES` and `TERR_PROFILES`. A1 and A2 therefore drive tc through
byte-identical code, so any measured difference is attributable to
controller placement and not to a divergent forwarding plane. Do not
"clean this up" by duplicating the logic — that silently invalidates the
comparison.

Link emulation (NetEm profile + terrestrial rate cap, keyed off
`/tmp/current_condition.txt`) also lives in the daemon. It is testbed
scaffolding, not a control decision, and must behave identically under both
architectures.

Cadence: E2 indications arrive every 10 ms, but inference is rate-limited
to `CONTROL_PERIOD=1.0` s to match A1's `LOOP_INTERVAL`. Without that, A2
would be a 100x faster controller and the comparison would measure cadence
rather than placement. Hysteresis/dwell exists (`DWELL_SEC`) but defaults
to 0 for the same reason — A1 has none.

## Results

### Cross-architecture comparison (127/128 cells)

QoS satisfaction, scored against Table 1.4 5QI thresholds (delay <= PDB,
loss <= PER, **and** goodput >= offered rate):

| Scheme | Clear:Unimp | Stressed3:Unimp | Clear:Sev-loss | Stressed3:Worst-loss | Total |
|---|---|---|---|---|---|
| TN-only | 75% | 25% | 75% | 29% | 52% |
| NTN-only | 75% | 75% | 0% | 12% | 41% |
| Fixed rule | 75% | 75% | 25% | 25% | 50% |
| **ML** | 75% | 75% | **75%** | 25% | **62%** |

The entire 12-point ML margin comes from `Clear:Sev-loss`, where the fixed
rule routes Video and Background onto a 3%-loss NTN path on class label
alone while the ML controller keeps them terrestrial. That is the
link-condition awareness the study exists to isolate.

### Decision-path latency (section 6.3)

| Stage | A1 | A2 |
|---|---|---|
| E2 arrival | — | 0.11 ms |
| Inference (`predict` + `predict_proba`) | 4.1 ms | 13.5 ms |
| Decision -> enforcement | ~16 ms | ~16 ms (shared code) |
| First packet on new path | — | 1.9 ms |
| Observable switch latency (T0->T6) | — | ~926 ms |

Observable switch latency is ~94% control period, not mechanism.

### Stress tests (section 4.10)

All six pass. Notably: E2 reporting at 1 ms vs 10 ms left gNB CPU unchanged
(110%) and RIC at 0.2%; only the xApp rose (18.8% -> 30%). Killing the xApp
leaves tc filters untouched (last-known-good holds). Removing the decision
source puts the daemon in `state: "stale"` while holding all four policies.

## Caveats — read before quoting any number

1. **The xApp does not use E2 data to decide.** The E2 indication is only a
   clock tick. Model features still come from the static per-condition
   lookup in `master_metrics.csv`, exactly as in A1. This is deliberate
   (guide 4.6: keep the A1 feature set or the comparison is confounded) and
   the "path telemetry sidecar" of 4.1 does not exist. Supportable claim:
   "a learned policy executes inside a FlexRIC xApp and steers traffic per
   class". Unsupportable: "the controller decides from RAN telemetry
   delivered over E2".

2. **The +9.4 ms in-xApp inference penalty is unexplained.** Same model,
   same two calls: 4.1 ms standalone vs 13.5 ms inside the xApp. The
   hypothesis is GIL contention with the 100 Hz indication callback, but it
   was never tested. It is falsifiable by varying `E2_INTERVAL` and seeing
   whether inference latency tracks indication rate.

3. **T6 instrumentation perturbs enforcement.** Enforcement measured ~16 ms
   without the T6 watcher and ~42.5 ms with it, because the watcher threads
   spawn `tc` subprocesses that contend with the `tc filter` calls doing the
   work. Quote enforcement from uninstrumented runs; treat
   `switch_latency.csv` as valid only for the T5->T6 column.

4. **`switch_latency.csv` needs a filter.** Rows where `observable_us`
   >= 5e6 are artifacts: T0 is the condition file's mtime, so a decision
   made against a long-unchanged condition reports "time since the path
   state last changed" (~917 s), not switch latency. 4 of 12 rows in the
   current file. Any automated analysis must exclude them.

5. **Haptic-EF fails in every cell.** Its 5QI 87 budget is 5 ms PDB, below
   the ~22-45 ms end-to-end RAN baseline, so it can never comply regardless
   of routing. The achievable QoS ceiling in this testbed is 75%, not 100%.
   This confirms the thesis section 1.5 reasoning with measurements.

6. **The A1 column was never independently measured.** The ML rows ran
   through A2's enforcement path. A1/A2 data-plane parity is inferred from
   24/24 identical decisions plus shared `update_tc_filter` — reasonable but
   not measured. To close: start `sudo -E python3
   ~/architecture_1/deploy_controller.py` and re-run only the ML rows.

7. **Condition selection matters more than it looks.** The first two
   conditions both had an unimpaired NTN path, so NTN was never the wrong
   choice and ML, fixed-rule and NTN-only all scored an identical 75%. The
   comparison only produced a result after adding conditions where NTN is
   degraded. Any new condition set needs at least one case where the NTN
   path is clearly worse.

8. **One cell is missing** (`tn-only / Stressed3:Worst-loss`), lost to a UE
   drop. That row is 2/7, not 2/8.

## Files

```
~/architecture_1/           A1: collection, labelling, training, deploy_controller.py
                            training_data/model.pkl  <- the model both architectures use
                            bin_a1/                  <- pre-E2 binaries, A1's validated build
~/architecture_2/
  BUILD_NOTES.md            nine build workarounds, with verified line refs
  HANDOFF.md                this file
  ml_xapp.py                the xApp (decision)
  run_ml_xapp.sh            launcher; sets the required LD_PRELOAD
  steer_daemon.py           the daemon (enforcement, root)
  steer_client.py           test/stress driver, stands in for the xApp
  stress_tests.py           section 4.10 tests
  compare_arch.py           section 6 comparison matrix (resumable)
  install_flexric_paths.sh  one-time sudo step for the Python SDK
  start_gnb_e2.sh           gNB with the e2_agent config
  start_ue.sh               UE (unchanged from A1)
  libasn1_nr_rrc.so         shared build of the static NR RRC ASN.1 archive
  flexric_sm/               8 service models (RIC loads from here)
  comparison_results.csv    section 6.2 data
  xapp_timing.csv           T1..T4 per decision
  steer_timing.csv          T4..T5 per decision
  switch_latency.csv        T0..T6 per switch (see caveat 4)
~/ue_supervisor.sh          restarts the UE on drop; PROACTIVE_AGE=330 cycles pre-emptively
~/ue_guard.sh               generation counter; rejects runs spanning a UE restart
```

## Run order

```bash
cd ~/openairinterface5g/openair2/E2AP/flexric/build/examples/ric
./nearRT-RIC -p ~/architecture_2/flexric_sm/ -c ~/architecture_2/flexric.conf

sudo ~/architecture_2/start_gnb_e2.sh      # separate terminal
sudo ~/architecture_2/start_ue.sh          # separate terminal
sudo -E python3 ~/architecture_2/steer_daemon.py
~/architecture_2/run_ml_xapp.sh
```

`sudo -E` on the daemon is required: it imports A1's controller, which
imports pandas from the user site-packages. Plain `sudo` gives
`ModuleNotFoundError: No module named 'pandas'`.

Before the first run after a reboot, re-run
`sudo ~/architecture_1/setup_steering.sh` — the iptables marking rules do
not persist. The `/usr/local/lib/flexric/` install does persist.

## State at handoff

- gNB and UE were **down**; RIC, steering daemon and UE supervisor still up.
  Restart the gNB then the UE to resume.
- The UE loses sync roughly every 6 minutes by design (NTN drift). Long runs
  need `ue_supervisor.sh`. See the `ntn-ue-restart` memory.
