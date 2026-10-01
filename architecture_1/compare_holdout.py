#!/usr/bin/env python3
"""
compare_holdout.py -- test the trained model against independently
collected data.

Every validation in this project so far holds out link conditions from
WITHIN a single collection session. This compares against a batch measured
in a different session, on a different day, with different UE instances --
so it tests generalisation across measurement epochs, not just across link
states. That is the closest thing to a true held-out test set available
here.

Three checks, in order of what they can invalidate:

  1. MEASUREMENT REPEATABILITY  do the same configured conditions produce
     the same RTT / loss / goodput in a fresh session? If not, the model
     may be fitted to session-specific noise and everything downstream is
     weaker than it looks.

  2. LABEL STABILITY            do the oracle labels derived from the
     fresh measurements match the originals? A cell that flips was near a
     decision boundary.

  3. MODEL ACCURACY ON FRESH DATA
     feed fresh feature vectors to the saved model and score against
     fresh labels.

SAFETY: read-only with respect to training_data/. This script never
rebuilds master_metrics.csv, never retrains, and never writes a model.
The hold-out batch lives in holdout_batch/, which deliberately does not
match the ntn_data_[0-9]* glob merge_dataset.py globs for.

Terrestrial metrics are reused from the existing dataset -- only the NTN
half is re-measured, because re-collecting terrestrial would require the
band-78 gNB swap and that half is the stable one (72/72 runs, no UE drops).

Run:  python3 ~/architecture_1/compare_holdout.py
"""
import glob
import importlib.util
import os
import pickle
import re
import sys

import pandas as pd

HOLDOUT_DIR = os.path.expanduser("~/architecture_1/holdout_batch")
TRAIN_DIR = os.path.expanduser("~/architecture_1/training_data")
MASTER = os.path.join(TRAIN_DIR, "master_metrics.csv")
LABELS = os.path.join(TRAIN_DIR, "decision_labels.csv")
MODEL = os.path.join(TRAIN_DIR, "model.pkl")
MERGE = os.path.expanduser("~/architecture_1/merge_dataset.py")
OUT_CSV = os.path.expanduser("~/results/tables/T9_holdout_comparison.csv")

# Same regexes as parse_ntn.py. The RECEIVER line is used, so throughput
# is the delivered rate -- consistent with how master_metrics.csv was
# built, which is what makes the two comparable at all.
SR = re.compile(r"([\d.]+)\s+([KMG]?)bits/sec\s+"
                r"([\d.]+)\s+ms\s+"
                r"(\d+)/(\d+)\s+\(([\d.]+)%\)\s+"
                r"(receiver|sender)")
RR = re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/")


def parse_holdout():
    """Parse the hold-out logs into per-(condition, class) means."""
    rows, missing = [], []
    for log in sorted(glob.glob(os.path.join(HOLDOUT_DIR, "*.log"))):
        stem = os.path.basename(log)[:-4]
        if stem.endswith("_ping"):
            continue
        cond = stem.rsplit("_rep", 1)[0] if "_rep" in stem else stem
        text = open(log).read()
        secs = re.split(r"--- Class: (.+?) \| Port: (\d+) \| Rate: (.+?) ---", text)
        for i in range(1, len(secs), 4):
            cname, body = secs[i].strip(), secs[i + 3]
            ms = list(SR.finditer(body))
            if not ms:
                missing.append(f"{stem} {cname}")
                continue
            m = next((x for x in reversed(ms) if x.group(7) == "receiver"), ms[-1])
            br, unit = float(m.group(1)), m.group(2)
            kbps = (br * 1000 if unit == "M" else br * 1e6 if unit == "G"
                    else br if unit == "K" else br / 1000)
            rtt = None
            p = os.path.join(HOLDOUT_DIR, f"{stem}_{cname}_ping.log")
            if os.path.exists(p):
                rm = RR.search(open(p).read())
                if rm:
                    rtt = float(rm.group(1))
            rows.append({"condition": cond.replace("NTN-", ""), "class": cname,
                         "rtt_ms": rtt, "loss_fraction": float(m.group(6)) / 100.0,
                         "throughput_kbps": kbps})
    if missing:
        print(f"  !! {len(missing)} run(s) produced no iperf3 summary:")
        for m in missing:
            print(f"       {m}")
    if not rows:
        sys.exit("No hold-out data parsed. Run collect_holdout.sh first.")
    df = pd.DataFrame(rows)
    return df.groupby(["condition", "class"], as_index=False).mean(numeric_only=True)


def main():
    if not os.path.isdir(HOLDOUT_DIR):
        sys.exit(f"{HOLDOUT_DIR} does not exist. Run collect_holdout.sh first.")

    print("=" * 70)
    print("HOLD-OUT COMPARISON -- independently collected batch")
    print("=" * 70)

    new = parse_holdout()
    master = pd.read_csv(MASTER)
    old = master[master.path == "NTN"].copy()
    conds = sorted(new.condition.unique())
    print(f"\nhold-out conditions: {', '.join(conds)}")
    print(f"cells re-measured  : {len(new)}\n")

    # ---- 1. measurement repeatability ---------------------------------
    print("-" * 70)
    print("1. MEASUREMENT REPEATABILITY  (original vs hold-out)")
    print("-" * 70)
    merged = new.merge(old, on=["condition", "class"], suffixes=("_new", "_old"))
    if merged.empty:
        sys.exit("No overlapping cells -- check condition names.")
    merged["d_rtt"] = merged.rtt_ms_new - merged.rtt_ms_old
    merged["d_loss_pp"] = 100 * (merged.loss_fraction_new - merged.loss_fraction_old)
    merged["d_thr_pct"] = 100 * (merged.throughput_kbps_new
                                 - merged.throughput_kbps_old) / merged.throughput_kbps_old
    print(f"{'condition':<20}{'class':<15}{'RTT old':>9}{'new':>9}{'delta':>8}"
          f"{'loss old':>10}{'new':>8}{'thr delta':>11}")
    for _, r in merged.sort_values(["condition", "class"]).iterrows():
        print(f"{r.condition:<20}{r['class']:<15}{r.rtt_ms_old:>8.1f}ms"
              f"{r.rtt_ms_new:>8.1f}ms{r.d_rtt:>+8.1f}"
              f"{100*r.loss_fraction_old:>9.2f}%{100*r.loss_fraction_new:>7.2f}%"
              f"{r.d_thr_pct:>+10.1f}%")
    print(f"\n  mean |RTT delta|      : {merged.d_rtt.abs().mean():.2f} ms")
    print(f"  mean |loss delta|     : {merged.d_loss_pp.abs().mean():.2f} pp")
    print(f"  mean |throughput drift|: {merged.d_thr_pct.abs().mean():.2f} %")
    print("\n  Interpretation: deltas within the run-to-run spread already seen")
    print("  in the original collection mean the testbed is stable across")
    print("  sessions. Large drift would mean the model is fitted to")
    print("  session-specific conditions rather than to link state.")

    # ---- 2 & 3. labels and model on fresh data ------------------------
    # merge_dataset's own compliance and routing functions are imported
    # rather than reimplemented, so the hold-out labels are produced by
    # exactly the rule that produced the training labels.
    spec = importlib.util.spec_from_file_location("merge", MERGE)
    mg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mg)

    terr = master[master.path == "Terrestrial"].copy()
    art = pickle.load(open(MODEL, "rb"))
    pipe = art["pipeline"]

    recs = []
    for _, n in new.iterrows():
        n_comp = mg.compliant(n["class"], n.rtt_ms, n.loss_fraction, n.throughput_kbps)
        n_viol = mg.violation(n["class"], n.rtt_ms, n.loss_fraction, n.throughput_kbps)
        for _, t in terr[terr["class"] == n["class"]].iterrows():
            label, reason = mg.route(
                {"compliant": t.compliant, "throughput_kbps": t.throughput_kbps,
                 "violation_score": t.violation_score},
                {"compliant": n_comp, "throughput_kbps": n.throughput_kbps,
                 "violation_score": n_viol}, n["class"])
            recs.append({"class": n["class"], "terr_condition": t.condition,
                         "ntn_condition": n.condition,
                         "terr_rtt_ms": t.rtt_ms, "terr_loss": t.loss_fraction,
                         "terr_thr_kbps": t.throughput_kbps,
                         "ntn_rtt_ms": n.rtt_ms, "ntn_loss": n.loss_fraction,
                         "ntn_thr_kbps": n.throughput_kbps,
                         "holdout_label": label, "reason": reason})
    h = pd.DataFrame(recs)

    print("\n" + "-" * 70)
    print("2. LABEL STABILITY  (hold-out labels vs original labels)")
    print("-" * 70)
    orig = pd.read_csv(LABELS)[["class", "terr_condition", "ntn_condition", "label"]]
    cmp_ = h.merge(orig, on=["class", "terr_condition", "ntn_condition"], how="inner")
    agree = (cmp_.holdout_label == cmp_.label)
    print(f"  labels agreeing: {agree.sum()}/{len(cmp_)}  ({100*agree.mean():.1f}%)")
    if (~agree).any():
        print("\n  cells that flipped (these sat near a decision boundary):")
        for _, r in cmp_[~agree].iterrows():
            print(f"    {r['class']:<15} {r.terr_condition:<11} {r.ntn_condition:<18}"
                  f" {r.label} -> {r.holdout_label}   ({r.reason})")

    print("\n" + "-" * 70)
    print("3. MODEL ACCURACY ON FRESH DATA")
    print("-" * 70)
    X = h[art["cat_cols"] + art["num_cols"]]
    pred = ["NTN" if p == 1 else "Terrestrial" for p in pipe.predict(X)]
    h["predicted"] = pred
    acc = (h.predicted == h.holdout_label).mean()
    print(f"  model vs hold-out labels : {acc:.4f}  ({(h.predicted==h.holdout_label).sum()}/{len(h)})")
    fixed = h["class"].map({"Voice-EF": "Terrestrial", "Haptic-EF": "Terrestrial",
                            "Video-AF41": "NTN", "Background-BE": "NTN"})
    print(f"  fixed rule vs same labels: {(fixed==h.holdout_label).mean():.4f}")
    print("\n  per held-out NTN condition:")
    for c, g in h.groupby("ntn_condition"):
        print(f"    {c:<20} {(g.predicted==g.holdout_label).mean():.3f}  ({len(g)} rows)")

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    h.to_csv(OUT_CSV, index=False)
    print(f"\nWrote {OUT_CSV}")
    print("\ntraining_data/ was NOT modified: no re-merge, no retrain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
