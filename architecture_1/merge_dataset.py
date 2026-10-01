#!/usr/bin/env python3
"""
merge_dataset.py -- build the training set from labelled terrestrial + NTN data.

Key fixes vs previous version:
  1. Compliance uses the FULL three-term rule:
       L_p <= PDB_c AND PLR_p <= PER_c AND T_p >= R_c
     (previous version omitted the throughput term).
  2. Both-compliant tie-break follows the documented rule (argmax throughput).
     Set TIEBREAK = "prefer_terrestrial" to revert (then update methodology).
  3. Emits group keys for grouped cross-validation, since rows are NOT
     independent (each terrestrial measurement is reused across NTN conditions).
"""
import os
import glob
import pandas as pd

OFFERED_RATE_KBPS = {
    "Voice-EF": 100.0,
    "Video-AF41": 1000.0,
    "Haptic-EF": 50.0,
    "Background-BE": 500.0,
}

THRESHOLDS = {
    "Voice-EF": (100.0, 1e-2),
    "Video-AF41": (150.0, 1e-3),
    "Haptic-EF": (5.0, 1e-3),
    "Background-BE": (None, None),
}

TIEBREAK = "argmax_throughput"
THROUGHPUT_TOLERANCE = 0.02


def latest(pattern):
    dirs = sorted(
        glob.glob(os.path.expanduser(pattern)),
        key=lambda p: int(p.rsplit("_", 1)[1]),
    )
    if not dirs:
        raise SystemExit(f"No directories matching {pattern}")
    return dirs[-1]


def compliant(cls, rtt_ms, loss_fraction, throughput_kbps):
    pdb, per = THRESHOLDS[cls]
    rate = OFFERED_RATE_KBPS[cls]
    delay_ok = (pdb is None) or (pd.notna(rtt_ms) and rtt_ms <= pdb)
    loss_ok = (per is None) or (loss_fraction <= per)
    rate_ok = (
        pd.notna(throughput_kbps)
        and throughput_kbps >= rate * (1.0 - THROUGHPUT_TOLERANCE)
    )
    return bool(delay_ok and loss_ok and rate_ok)


def violation(cls, rtt_ms, loss_fraction, throughput_kbps):
    """Normalised shortfall against the class's QoS targets.

    Includes a THROUGHPUT term, so this matches the three-term rule used
    by compliant(). Without it the function was internally inconsistent:
    a class could be judged non-compliant on throughput yet score a
    violation of exactly 0.

    That inconsistency was not cosmetic. Background-BE has no PDB and no
    PER (THRESHOLDS below), so both of the other terms were skipped and
    its violation score was always 0.0. In route()'s "neither compliant"
    branch the tie-break is `ntn_violation < terr_violation`, which for
    Background-BE was always `0 < 0` -> false -> default Terrestrial. The
    result was labels like:

        Background-BE  Stressed3/Sev-loss   terr_loss 78.3%  ntn_loss 3.0%
                                            -> labelled Terrestrial

    i.e. a path dropping 78% of packets preferred over one dropping 3%,
    by default rather than by judgement. It affected 9 rows, all
    Background-BE, and was the reason every model family failed the
    held-out Sev-loss fold at exactly 0.833 -- the models were right and
    the labels were wrong.

    The throughput term is normalised the same way as the others (relative
    shortfall against the offered rate), so for classes that already have
    a PDB and PER it is small and only breaks ties the delay and loss
    terms leave equal.
    """
    pdb, per = THRESHOLDS[cls]
    rate = OFFERED_RATE_KBPS[cls]
    v = 0.0
    if pdb is not None and pdb > 0 and pd.notna(rtt_ms):
        v += max(0.0, (rtt_ms - pdb) / pdb)
    if per is not None and per > 0:
        v += max(0.0, (loss_fraction - per) / per)
    if rate > 0 and pd.notna(throughput_kbps):
        v += max(0.0, (rate - throughput_kbps) / rate)
    return v


def route(t_row, n_row, cls):
    t_c, n_c = t_row["compliant"], n_row["compliant"]
    if t_c and not n_c:
        return "Terrestrial", "only_terrestrial_compliant"
    if n_c and not t_c:
        return "NTN", "only_ntn_compliant"
    if t_c and n_c:
        if TIEBREAK == "prefer_terrestrial":
            return "Terrestrial", "both_compliant_prefer_terrestrial"
        if n_row["throughput_kbps"] > t_row["throughput_kbps"]:
            return "NTN", "both_compliant_ntn_higher_throughput"
        return "Terrestrial", "both_compliant_terr_higher_or_equal_throughput"
    if n_row["violation_score"] < t_row["violation_score"]:
        return "NTN", "neither_compliant_ntn_lower_violation"
    return "Terrestrial", "neither_compliant_terr_lower_or_equal_violation"


def load_path(dir_pattern, csv_name, prefix, path_label):
    d = latest(dir_pattern)
    df = pd.read_csv(os.path.join(d, csv_name))
    df = df.rename(columns={"class_": "class"})
    df["condition"] = df["condition"].str.replace(prefix, "", regex=False)
    df["path"] = path_label
    df["compliant"] = [
        compliant(r["class"], r["rtt_ms"], r["loss_fraction"], r["throughput_kbps"])
        for _, r in df.iterrows()
    ]
    df["violation_score"] = [
        violation(r["class"], r["rtt_ms"], r["loss_fraction"],
                  r["throughput_kbps"])
        for _, r in df.iterrows()
    ]
    print(f"{path_label}: {len(df)} rows  (from {d})")
    return df


def main():
    print(f"Tie-break policy: {TIEBREAK}")
    print(f"Throughput tolerance: {THROUGHPUT_TOLERANCE:.0%} of offered rate\n")

    t = load_path("~/architecture_1/terrestrial_data_[0-9]*", "labelled_terrestrial.csv",
                  "Terr-", "Terrestrial")
    n = load_path("~/architecture_1/ntn_data_[0-9]*", "labelled_ntn.csv",
                  "NTN-", "NTN")

    cols = ["class", "condition", "path", "rtt_ms", "jitter_ms",
            "loss_fraction", "throughput_kbps", "compliant", "violation_score"]
    master = pd.concat([t[cols], n[cols]], ignore_index=True)

    rows = []
    for cls in sorted(master["class"].unique()):
        t_rows = master[(master["class"] == cls) & (master["path"] == "Terrestrial")]
        n_rows = master[(master["class"] == cls) & (master["path"] == "NTN")]
        for _, tr in t_rows.iterrows():
            for _, nr in n_rows.iterrows():
                label, reason = route(tr, nr, cls)
                rows.append({
                    "class": cls,
                    "terr_condition": tr["condition"],
                    "ntn_condition": nr["condition"],
                    "terr_rtt_ms": tr["rtt_ms"],
                    "terr_loss": tr["loss_fraction"],
                    "terr_thr_kbps": tr["throughput_kbps"],
                    "ntn_rtt_ms": nr["rtt_ms"],
                    "ntn_loss": nr["loss_fraction"],
                    "ntn_thr_kbps": nr["throughput_kbps"],
                    "terr_compliant": tr["compliant"],
                    "ntn_compliant": nr["compliant"],
                    "terr_violation": tr["violation_score"],
                    "ntn_violation": nr["violation_score"],
                    "decision_reason": reason,
                    "group_terr": f"{cls}|{tr['condition']}",
                    "group_ntn": f"{cls}|{nr['condition']}",
                    "label": label,
                })

    dec = pd.DataFrame(rows).sort_values(
        ["class", "terr_condition", "ntn_condition"]
    ).reset_index(drop=True)

    out_dir = os.path.expanduser("~/architecture_1/training_data")
    os.makedirs(out_dir, exist_ok=True)
    master.to_csv(os.path.join(out_dir, "master_metrics.csv"), index=False)
    dec.to_csv(os.path.join(out_dir, "decision_labels.csv"), index=False)

    print(f"\nmaster_metrics.csv : {len(master)} rows (independent measurements)")
    print(f"decision_labels.csv: {len(dec)} rows (cross-joined pairs -- NOT independent)")
    print(f"\nLabel distribution:\n{dec['label'].value_counts()}")
    print(f"\nDecision reasons:\n{dec['decision_reason'].value_counts()}")
    print(f"\nNTN labels per class:")
    ntn_only = dec[dec["label"] == "NTN"]
    if len(ntn_only):
        print(ntn_only.groupby("class").size().to_string())
    else:
        print("  (none)")


if __name__ == "__main__":
    main()
