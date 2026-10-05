# Edge AI-Based Traffic Offloading for NTN Backhaul Links

EEE4022S final-year project, University of Cape Town.
Supervisor: Prof. Joyce Mwangama.

An OpenAirInterface 5G SA testbed that steers per-class traffic between a
terrestrial backhaul and an emulated LEO non-terrestrial (NTN) backhaul,
using a supervised ML controller trained on measured link state.

This repository holds **scripts, configuration and build notes**. Collected data,
trained models and results are kept out of version control.

## Layout

`architecture_2/` has a `BUILD_NOTES.md` recording how it was built, the errors hit along the way and how they were fixed.

| Directory | Contents |
|---|---|
| `architecture_1/` | Host-level controller: data collection, parsing, labelling, dataset merge, training, deployment, hold-out test |
| `architecture_2/` | Monolithic gNB with E2 agent, FlexRIC xApp, steering daemon/client, stress tests, concurrent traffic mix |
| `oai_conf/` | gNB configs: band 254 LEO NTN and band 78 terrestrial |

## Pipeline (Architecture 1)

```
collect_terrestrial.sh / collect_ntn.sh   ->  raw iperf3 + ping logs
parse_terrestrial.py   / parse_ntn.py     ->  per-run metrics
merge_dataset.py                          ->  master_metrics.csv, decision_labels.csv
train_controller.py                       ->  model.pkl (+ model comparison, LOCO tests)
deploy_controller.py                      ->  live steering via tc/iptables
collect_holdout.sh + compare_holdout.py   ->  fresh-session hold-out test
```

## Requirements

- OpenAirInterface 5G (RFsimulator, built with NTN support)
- FlexRIC (Architectures 2 )
- Python 3 with pandas and scikit-learn
- iperf3, Linux `tc` / `netem`, iptables

Scripts assume they run from `~/architecture_N/` and that the OAI tree is at
`~/openairinterface5g`.

## Keeping this repo up to date

The live testbed scripts hardcode `~/architecture_N` paths, so they stay
there and this repo holds copies. To pick up changes:

```bash
./sync.sh && git add -A && git commit -m "sync" && git push
```

The UE credentials in the configs are the standard OAI test vectors on the
reserved `001/01` test PLMN, not real subscriber secrets.
