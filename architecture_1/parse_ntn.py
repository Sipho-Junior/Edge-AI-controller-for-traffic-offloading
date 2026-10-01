import os, re, csv, glob
from pathlib import Path

dirs = sorted(glob.glob(os.path.expanduser("~/architecture_1/ntn_data_[0-9]*")),
              key=lambda p: int(p.rsplit("_",1)[1]))
D = dirs[-1]
OUT = os.path.join(D, "parsed_ntn.csv")
print("Using folder:", D)

T = {"Voice-EF":(100,0.01),"Video-AF41":(150,0.001),
     "Haptic-EF":(5,0.001),"Background-BE":(None,None)}

SR = re.compile(
    r"([\d.]+)\s+([KMG]?)bits/sec\s+"
    r"([\d.]+)\s+ms\s+"
    r"(\d+)/(\d+)\s+\(([\d.]+)%\)\s+"
    r"(receiver|sender)"
)
RR = re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/")

rows=[]
missing=[]   # class sections that produced no iperf3 summary line
for log in sorted(Path(D).glob("*.log")):
    stem=log.stem
    if stem.endswith("_ping"): continue
    cond, rep = stem.rsplit("_rep",1) if "_rep" in stem else (stem,"0")
    text=log.read_text()
    secs=re.split(r"--- Class: (.+?) \| Port: (\d+) \| Rate: (.+?) ---", text)
    for i in range(1,len(secs),4):
        cname=secs[i].strip(); body=secs[i+3]
        ms=list(SR.finditer(body))
        if not ms:
            # iperf3 prints its summary over the TCP control connection at
            # the end of the run. Under an intermittent profile that
            # exchange can land inside an outage window, and the run then
            # yields no summary at all. Silently skipping it would drop
            # the measurement from the dataset without trace -- and a
            # condition/class cell missing from master_metrics.csv makes
            # build_features() return valid=False at deployment, so the
            # controller just ignores that class. Report it instead.
            missing.append(f"{cond} rep{rep} {cname}")
            continue
        m=next((x for x in reversed(ms) if x.group(7)=="receiver"), ms[-1])
        br=float(m.group(1)); u=m.group(2)
        jt=float(m.group(3))
        lt=int(m.group(4)); tt=int(m.group(5))
        lp=float(m.group(6))
        rtt=None
        p=Path(f"{log.parent}/{stem}_{cname}_ping.log")
        if p.exists():
            rm=RR.search(p.read_text())
            if rm: rtt=float(rm.group(1))
        kbps = br*1000 if u=="M" else br*1e6 if u=="G" else br if u=="K" else br/1000
        pdb,per = T.get(cname,(None,None))
        rows.append(dict(condition=cond,repetition=rep,class_=cname,source=m.group(7),
                         rtt_ms=rtt,jitter_ms=jt,loss_pct=lp,loss_fraction=lp/100,
                         throughput_kbps=kbps,lost=lt,total=tt,pdb_ms=pdb,per=per))

if missing:
    print(f"\n!!! {len(missing)} class run(s) produced no iperf3 summary "
          f"and were NOT parsed:")
    for m in missing:
        print(f"      {m}")
    print("    Re-run those reps before training, or the affected "
          "(class, condition) cells\n    will be absent from "
          "master_metrics.csv and skipped at deployment.\n")

# Every (condition, class) pair must survive parsing, or that cell is
# missing downstream. Report the shape so a gap is visible immediately.
seen = {}
for r in rows:
    seen.setdefault(r["condition"], set()).add(r["class_"])
for cond in sorted(seen):
    n = len(seen[cond])
    flag = "" if n == 4 else f"   <-- only {n}/4 classes"
    print(f"  {cond:<24} {n}/4 classes{flag}")

if not rows:
    print("No data parsed."); raise SystemExit(1)

with open(OUT,"w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print(f"Parsed {len(rows)} rows -> {OUT}")
