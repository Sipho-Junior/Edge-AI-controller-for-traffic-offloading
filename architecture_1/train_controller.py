#!/usr/bin/env python3
"""
train_controller.py -- train and honestly validate the offloading controller.
"""
import os
import pickle

import numpy as np
import pandas as pd
import sklearn
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import (
    LeaveOneOut, LeaveOneGroupOut, cross_val_predict,
)
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, confusion_matrix,
    classification_report,
)

MAX_DEPTH = 4
RANDOM_STATE = 42

CAT_COLS = ["class", "terr_condition", "ntn_condition"]
NUM_COLS = ["terr_rtt_ms", "terr_loss", "terr_thr_kbps",
            "ntn_rtt_ms", "ntn_loss", "ntn_thr_kbps"]

FORBIDDEN = ["label", "decision_reason",
             "compliant", "violation_score",
             "terr_compliant", "ntn_compliant",
             "terr_violation", "ntn_violation"]

FIXED_RULE = {
    "Voice-EF": "Terrestrial",
    "Haptic-EF": "Terrestrial",
    "Video-AF41": "NTN",
    "Background-BE": "NTN",
}


def build_pipeline(clf=None, scale=False):
    """Feature pipeline + classifier.

    scale=True adds StandardScaler to the numeric block. Trees are
    scale-invariant and do not need it; LogisticRegression does -- the numeric
    features span rtt_ms (tens), loss (1e-4..0.2) and thr_kbps (tens to
    ~1000), so an unscaled linear model is dominated by throughput purely
    because of its magnitude.
    """
    num_tf = Pipeline([("sc", StandardScaler())]) if scale else "passthrough"
    pre = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_COLS),
            ("num", num_tf, NUM_COLS),
        ]
    )
    if clf is None:
        clf = DecisionTreeClassifier(
            max_depth=MAX_DEPTH,
            class_weight="balanced",
            min_impurity_decrease=0.001,
            random_state=RANDOM_STATE,
        )
    return Pipeline([("pre", pre), ("clf", clf)])


# Candidate model types for the "which learning approach suits this
# decision" comparison (proposal bullet 5; implementation guide 2.7).
#
# Each entry is (classifier, needs_scaling, deployment note). The
# deployment note matters as much as the accuracy: guide 2.7 and
# Obiuwevwi et al. [4] both judge a model by whether it can be exported
# into an xApp, not by test score alone.
MODELS = {
    "DecisionTree (depth 4)": (
        DecisionTreeClassifier(max_depth=MAX_DEPTH, class_weight="balanced",
                               min_impurity_decrease=0.001,
                               random_state=RANDOM_STATE),
        False, "trivial to export; currently deployed"),
    "LogisticRegression": (
        LogisticRegression(class_weight="balanced", max_iter=5000,
                           random_state=RANDOM_STATE),
        True, "smallest C export; [4] demonstrates it in a FlexRIC xApp"),
    "RandomForest (200 trees)": (
        RandomForestClassifier(n_estimators=200, max_depth=MAX_DEPTH,
                               class_weight="balanced",
                               random_state=RANDOM_STATE, n_jobs=-1),
        False, "heavier C export than a single tree (guide 2.7)"),
}


def evaluate(name, X, y, cv, groups=None):
    pipe = build_pipeline()
    y_pred = cross_val_predict(pipe, X, y, cv=cv, groups=groups)
    acc = accuracy_score(y, y_pred)
    bal = balanced_accuracy_score(y, y_pred)
    cm = confusion_matrix(y, y_pred, labels=[0, 1])
    report = classification_report(
        y, y_pred, labels=[0, 1],
        target_names=["Terrestrial", "NTN"], zero_division=0,
    )
    n_splits = cv.get_n_splits(X, y, groups)

    print(f"--- {name} ---")
    print(f"folds              : {n_splits}")
    print(f"accuracy           : {acc:.3f}")
    print(f"balanced accuracy  : {bal:.3f}")
    print(f"confusion [[TN FP],[FN TP]]:\n{cm}")
    print(report)
    return {"name": name, "n_splits": n_splits, "accuracy": acc,
            "balanced_accuracy": bal, "confusion": cm, "report": report,
            "y_pred": y_pred}


def main():
    print(f"scikit-learn {sklearn.__version__} | pandas {pd.__version__}\n")

    data_path = os.path.expanduser("~/architecture_1/training_data/decision_labels.csv")
    df = pd.read_csv(data_path)
    print(f"Loaded {len(df)} rows from {data_path}")
    print(f"Label distribution:\n{df['label'].value_counts()}\n")

    for col in FORBIDDEN:
        assert col not in CAT_COLS and col not in NUM_COLS, \
            f"LEAK: {col} must not be a feature"
    missing = [c for c in CAT_COLS + NUM_COLS if c not in df.columns]
    assert not missing, f"Missing feature columns: {missing}"
    print("Leak check passed.\n")

    X = df[CAT_COLS + NUM_COLS]
    y = (df["label"] == "NTN").astype(int).values

    n_pos, n_neg = int(y.sum()), int((y == 0).sum())
    print(f"Samples: {len(df)} | NTN: {n_pos} | Terrestrial: {n_neg}")

    for gcol in ("group_terr", "group_ntn"):
        if gcol in df.columns:
            print(f"{gcol}: {df[gcol].nunique()} distinct groups")
    print()

    y_fixed = (df["class"].map(FIXED_RULE) == "NTN").astype(int).values
    fixed_acc = accuracy_score(y, y_fixed)
    fixed_bal = balanced_accuracy_score(y, y_fixed)
    print(f"Fixed-rule benchmark : accuracy {fixed_acc:.3f} | "
          f"balanced {fixed_bal:.3f}\n")

    results = []
    results.append(evaluate("Naive LOOCV (OPTIMISTIC -- see docstring)",
                            X, y, LeaveOneOut()))

    if "group_terr" in df.columns:
        results.append(evaluate("Leave-one-terrestrial-measurement-out",
                                X, y, LeaveOneGroupOut(),
                                groups=df["group_terr"].values))
    if "group_ntn" in df.columns:
        results.append(evaluate("Leave-one-NTN-measurement-out",
                                X, y, LeaveOneGroupOut(),
                                groups=df["group_ntn"].values))

    # ---- Generalisation to conditions never seen during training --------
    #
    # The grouped tests above hold out one (class, condition) CELL, but the
    # same condition is still present via the other three classes, so its
    # one-hot category is never actually novel to the model. Holding out a
    # whole CONDITION removes every row for that profile, so the model has
    # to predict from measured link state alone.
    #
    # This is the test the project proposal asks for: "evaluate cross
    # condition generalisation by testing the trained controller against
    # LEO impairment profiles not seen during training".
    #
    # It is only informative because the fitted tree branches on numeric
    # features rather than the condition one-hots. An unseen category
    # encodes to all-zeros under handle_unknown="ignore", so a model that
    # had really learned a per-condition lookup would collapse here --
    # which makes this simultaneously a generalisation measurement and a
    # check that the model is not memorising condition identity.
    generalisation = []
    generalisation.append(evaluate(
        "UNSEEN NTN profile (leave-one-ntn-condition-out)",
        X, y, LeaveOneGroupOut(), groups=df["ntn_condition"].values))
    generalisation.append(evaluate(
        "UNSEEN terrestrial level (leave-one-terr-condition-out)",
        X, y, LeaveOneGroupOut(), groups=df["terr_condition"].values))

    # Per-profile breakdown: an aggregate number hides the case that
    # matters, which is whether any single held-out profile is predicted
    # badly. Report each fold separately.
    print("=" * 62)
    print("GENERALISATION -- accuracy per held-out profile")
    print("=" * 62)
    for gcol, res in (("ntn_condition", generalisation[0]),
                      ("terr_condition", generalisation[1])):
        print(f"\nheld-out {gcol}:")
        yp = res["y_pred"]
        for cond in sorted(df[gcol].unique()):
            m = (df[gcol] == cond).values
            n = int(m.sum())
            a = accuracy_score(y[m], yp[m])
            n_ntn = int(y[m].sum())
            flag = "   <-- weakest" if a < 0.80 else ""
            print(f"  {cond:<22} {a:.3f}  ({n} rows, {n_ntn} labelled NTN){flag}")

    # ---- Model-type comparison (proposal bullet 5) ----------------------
    #
    # Judged on the UNSEEN-condition test, not the in-sample one: the
    # question is which learning approach transfers to a link state it has
    # not been trained on, which is what a deployed controller faces.
    #
    # Per-fold spread is reported, not just the mean. With 216 rows cross-
    # joined from 60 independent measurements, a 1-2 point difference in
    # mean accuracy is not evidence that one model is better; a model that
    # is consistently good across all 9 held-out profiles is a different
    # claim from one that averages well while failing badly on one.
    print("=" * 78)
    print("MODEL COMPARISON -- generalisation to an unseen NTN profile")
    print("=" * 78)
    print(f"{'model':<26}{'mean':>7}{'worst':>7}{'median':>8}{'#folds<0.9':>12}  note")
    print("-" * 78)
    comparison = []
    for mname, (clf, needs_scale, note) in MODELS.items():
        pipe = build_pipeline(clf, scale=needs_scale)
        yp = cross_val_predict(pipe, X, y, cv=LeaveOneGroupOut(),
                               groups=df["ntn_condition"].values)
        per_fold = []
        for cond in sorted(df["ntn_condition"].unique()):
            m = (df["ntn_condition"] == cond).values
            per_fold.append((cond, accuracy_score(y[m], yp[m])))
        accs = [a for _, a in per_fold]
        mean_a = float(np.mean(accs))
        worst_a = min(accs)
        med_a = float(np.median(accs))
        n_bad = sum(1 for a in accs if a < 0.9)
        worst_cond = min(per_fold, key=lambda x: x[1])[0]
        comparison.append({"name": mname, "mean": mean_a, "worst": worst_a,
                           "median": med_a, "n_bad": n_bad,
                           "worst_cond": worst_cond, "per_fold": per_fold,
                           "overall": accuracy_score(y, yp), "note": note})
        print(f"{mname:<26}{mean_a:>7.3f}{worst_a:>7.3f}{med_a:>8.3f}"
              f"{n_bad:>12}  {note}")
    print()
    print("weakest held-out profile per model:")
    for c in comparison:
        print(f"  {c['name']:<26} {c['worst_cond']} ({c['worst']:.3f})")
    print(f"\nfixed-rule benchmark for reference: {fixed_acc:.3f}")
    print("=" * 78 + "\n")

    grouped = [r for r in results if r["name"].startswith("Leave-one-")]
    print("=" * 62)
    print("SUMMARY")
    print("=" * 62)
    print(f"{'Fixed-rule benchmark':<46} {fixed_acc:.3f}")
    for r in results:
        print(f"{r['name']:<46} {r['accuracy']:.3f}")
    if grouped:
        best = min(r["accuracy"] for r in grouped)
        print(f"\nHeadline (most conservative grouped estimate): {best:.3f}")
        print(f"Delta vs fixed rule: {best - fixed_acc:+.3f}")
    if generalisation:
        # Reported separately, NOT folded into the headline: this answers a
        # different question (does the policy transfer to an unseen link
        # condition?) from the grouped estimate above (how accurate is it on
        # conditions it has seen?). Collapsing them into one number would
        # make the headline incomparable with the pre-intermittency result.
        gbest = min(r["accuracy"] for r in generalisation)
        print(f"\nGeneralisation to unseen conditions (worst case): {gbest:.3f}")
        for r in generalisation:
            print(f"  {r['name']:<52} {r['accuracy']:.3f}")
        print(f"Delta vs fixed rule: {gbest - fixed_acc:+.3f}")
    print("=" * 62 + "\n")

    # Persist every compared model, not only the deployed one. The
    # comparison in the block above fits each family inside
    # cross_val_predict and then discards it, so without this only the
    # DecisionTree survives the run. random_state is fixed, so these are
    # reproducible either way -- saving them just makes the artefacts
    # inspectable without a re-run.
    arch_dir = os.path.expanduser("~/architecture_1/model_archive")
    os.makedirs(arch_dir, exist_ok=True)
    for mname, (mclf, mscale, mnote) in MODELS.items():
        mp = build_pipeline(mclf, scale=mscale)
        mp.fit(X, y)
        slug = mname.split(" ")[0].lower()
        enc_m = mp.named_steps["pre"].named_transformers_["cat"]
        fn = list(enc_m.get_feature_names_out(CAT_COLS)) + NUM_COLS
        path = os.path.join(arch_dir, f"model.{slug}.pkl")
        with open(path, "wb") as f:
            pickle.dump({"pipeline": mp, "cat_cols": CAT_COLS,
                         "num_cols": NUM_COLS, "feature_names": fn,
                         "model_name": mname, "deployment_note": mnote}, f)
        print(f"Saved {mname:<26} -> {path}")

    pipe = build_pipeline()
    pipe.fit(X, y)

    enc = pipe.named_steps["pre"].named_transformers_["cat"]
    feature_names = list(enc.get_feature_names_out(CAT_COLS)) + NUM_COLS
    tree = pipe.named_steps["clf"]

    tree_text = export_text(tree, feature_names=feature_names)
    print("Decision tree (fitted on all data):")
    print(tree_text)

    importances = sorted(zip(feature_names, tree.feature_importances_),
                         key=lambda x: -x[1])
    print("Feature importances (non-zero):")
    for name, imp in importances:
        if imp > 0:
            print(f"  {name:<42} {imp:.3f}")
    print()

    out_dir = os.path.expanduser("~/architecture_1/training_data")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "model.pkl"), "wb") as f:
        pickle.dump({"pipeline": pipe,
                     "cat_cols": CAT_COLS,
                     "num_cols": NUM_COLS,
                     "feature_names": feature_names}, f)
    print(f"Model saved      -> {out_dir}/model.pkl")

    with open(os.path.join(out_dir, "cv_results.txt"), "w") as f:
        f.write(f"scikit-learn {sklearn.__version__} | pandas {pd.__version__}\n")
        f.write(f"Rows: {len(df)} | NTN: {n_pos} | Terrestrial: {n_neg}\n")
        f.write(f"Tree max_depth={MAX_DEPTH}, class_weight=balanced, "
                f"random_state={RANDOM_STATE}\n\n")
        f.write("NOTE: decision_labels.csv rows are cross-joined and therefore\n"
                "NOT independent. Naive LOOCV is optimistically biased; the\n"
                "grouped estimates below are the defensible ones.\n\n")
        f.write(f"Fixed-rule benchmark accuracy: {fixed_acc:.3f} "
                f"(balanced {fixed_bal:.3f})\n\n")
        for r in results:
            f.write(f"--- {r['name']} ---\n")
            f.write(f"folds: {r['n_splits']}\n")
            f.write(f"accuracy: {r['accuracy']:.3f}\n")
            f.write(f"balanced accuracy: {r['balanced_accuracy']:.3f}\n")
            f.write(f"confusion [[TN FP],[FN TP]]:\n{r['confusion']}\n")
            f.write(r["report"] + "\n")
        f.write("\nDecision tree:\n" + tree_text)
        f.write("\nFeature importances:\n")
        for name, imp in importances:
            if imp > 0:
                f.write(f"  {name}: {imp:.3f}\n")
    print(f"CV summary saved -> {out_dir}/cv_results.txt")

    preds = pd.DataFrame({"true": y, "fixed_rule": y_fixed})
    for r in results:
        key = ("loocv" if "LOOCV" in r["name"]
               else "grouped_terr" if "terrestrial" in r["name"]
               else "grouped_ntn")
        preds[key] = r["y_pred"]
    preds.to_csv(os.path.join(out_dir, "cv_predictions.csv"), index=False)
    print(f"Predictions saved-> {out_dir}/cv_predictions.csv")


if __name__ == "__main__":
    main()
