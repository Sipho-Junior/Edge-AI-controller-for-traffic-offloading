import pandas as pd, glob, os

dirs = sorted(glob.glob(os.path.expanduser("~/architecture_1/terrestrial_data_[0-9]*")),
              key=lambda p: int(p.rsplit("_",1)[1]))
D = dirs[-1]
df = pd.read_csv(os.path.join(D,"parsed_terrestrial.csv"))

agg = df.groupby(["condition","class_","pdb_ms","per"], dropna=False).agg(
    rtt_ms=("rtt_ms","mean"),
    jitter_ms=("jitter_ms","mean"),
    loss_fraction=("loss_fraction","mean"),
    throughput_kbps=("throughput_kbps","mean")
).reset_index()

labels=[]
for _,row in agg.iterrows():
    pdb=row["pdb_ms"]; per=row["per"]; rtt=row["rtt_ms"]
    cl = (pd.isna(pdb)) or (not pd.isna(rtt) and rtt<=pdb)
    cp = (pd.isna(per)) or (row["loss_fraction"]<=per)
    v = 0.0
    if not pd.isna(pdb) and pdb>0 and not pd.isna(rtt):
        v += max(0,(rtt-pdb)/pdb)
    if not pd.isna(per) and per>0:
        v += max(0,(row["loss_fraction"]-per)/per)
    labels.append(dict(class_=row["class_"],condition=row["condition"],
                       rtt_ms=round(rtt,3) if not pd.isna(rtt) else None,
                       jitter_ms=round(row["jitter_ms"],3),
                       loss_fraction=round(row["loss_fraction"],6),
                       throughput_kbps=round(row["throughput_kbps"],2),
                       pdb_ms=pdb,per=per,compliant=cl and cp,
                       violation_score=round(v,4)))

out=pd.DataFrame(labels).sort_values(["class_","condition"])
out_csv=os.path.join(D,"labelled_terrestrial.csv")
out.to_csv(out_csv,index=False)
print(f"Using folder: {D}")
print(f"Labelled data saved: {out_csv}")
print()
print(out.to_string(index=False))
