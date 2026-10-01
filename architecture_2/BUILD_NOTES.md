# Architecture 2 — build and run notes

Reproducibility notes for the OAI + FlexRIC ML xApp testbed. Every item
below was hit on a stock Ubuntu 22.04.5 install and none of them are
mentioned in the implementation guide. Host: Ubuntu 22.04.5 LTS, 12 cores,
15 GB RAM, gcc 11.4.0 default, cmake 3.22.1.

## 0. Version pairing (the one thing that worked first try)

OAI RAN and FlexRIC must be compiled with the same E2AP and E2SM-KPM
versions. Both default to a matching pair, so **no `--cmake-opt` flags are
needed**:

| Side | E2AP | KPM |
|---|---|---|
| OAI (`openair2/E2AP/README.md`) | v2.03 (default) | v2.03 |
| FlexRIC (`CMakeLists.txt:209,254`) | `E2AP_V2` | `KPM_V2_03` |

Both come from the same `duranta-project` fork, so they are a matched pair
rather than the mismatch risk the guide implies.

## 1. OAI with the E2 agent

```bash
cd ~/openairinterface5g
git submodule update --init openair2/E2AP/flexric     # submodule ships empty
cd cmake_targets
./build_oai --gNB --nrUE --build-e2 --ninja
```

Verify it actually took — an E2-less binary fails silently at runtime, not
at build time:

```bash
nm -C ran_build/build/nr-softmodem | grep -ci e2ap    # 0 before, ~2700 after
```

The binaries must not be running during the build: Linux returns `ETXTBSY`
when writing a running executable, so stop the gNB and UE first.

## 2. FlexRIC rejects gcc-11 — use gcc-12

Ubuntu 22.04's default compiler is gcc-11, and FlexRIC hard-fails on it
(`CMakeLists.txt:7`) because it uses C11 `_Generics` and nested functions.

```bash
cmake .. -DCMAKE_C_COMPILER=gcc-12
```

Note OAI's own build compiles the *same* FlexRIC sources under gcc-11
without complaint; only FlexRIC's standalone build refuses. Mixing
compilers is safe here because the gNB and RIC are separate processes
talking E2AP over a socket, not linked together.

## 3. SWIG ≥ 4.1 required; 22.04 ships 4.0.2

Needed only for the Python xApp bindings. `apt install swig` is **not
sufficient** and there is no newer package in the 22.04 repositories.
Build from source to a local prefix — no root required:

```bash
curl -L -o swig-4.1.1.tar.gz \
  https://github.com/swig/swig/archive/refs/tags/v4.1.1.tar.gz
tar xzf swig-4.1.1.tar.gz && cd swig-4.1.1
./autogen.sh
./configure --prefix=$HOME/.local --without-pcre
make -j$(nproc) && make install
```

`--without-pcre` avoids needing `libpcre2-dev`. It is safe here because
FlexRIC's `swig_wrapper.i` contains no regex `%rename` directives, which
are the only feature PCRE provides.

## 4. Python bindings are off by default, and cmake caches the SWIG version

Two separate traps:

```bash
cmake .. -DCMAKE_C_COMPILER=gcc-12 \
         -DXAPP_MULTILANGUAGE=ON \
         -USWIG_VERSION -USWIG_DIR \
         -DSWIG_EXECUTABLE=$HOME/.local/bin/swig
```

- `XAPP_MULTILANGUAGE` defaults to `OFF` (`CMakeLists.txt:145`), so the
  Python module is silently not built.
- cmake caches `SWIG_VERSION`. Without `-USWIG_VERSION -USWIG_DIR` it keeps
  reporting the old 4.0.2 even after `SWIG_EXECUTABLE` is repointed, and
  fails the version check against the new binary.

Build only the bindings target to avoid relinking a running RIC:

```bash
make xapp_sdk -j$(nproc)
```

## 5. `asn_DEF_NR_UL_DCCH_Message` undefined

`libe42_xapp_shared.so` references this OAI NR RRC ASN.1 symbol but does
not link it. FlexRIC provides it only as a **static** archive, so the C
xApps link it fine while the Python bindings cannot resolve it at load
time.

Build a shared object from the archive and preload it:

```bash
gcc-12 -shared -o libasn1_nr_rrc.so \
  -Wl,--whole-archive \
  <flexric>/build/examples/xApp/c/monitor/RRC_MESSAGES/libasn1_nr_rrc.a \
  -Wl,--no-whole-archive

export LD_PRELOAD=$PWD/libasn1_nr_rrc.so
```

Without this, `import xapp_sdk` fails with `undefined symbol`.

## 6. Keep `libe42_xapp_shared.so` out of the service-model directory

The Near-RT RIC `dlopen()`s **every** `.so` in its service-model directory.
`libe42_xapp_shared.so` has the unresolved symbol above, so its presence
aborts the RIC at startup:

```
plugin_ric.c:137: load_plugin_ric:
  Assertion `handle != NULL && "Could not open the file path"' failed.
```

Install only the 8 service models (`libkpm_sm.so`, `libmac_sm.so`,
`librlc_sm.so`, `libpdcp_sm.so`, `libgtp_sm.so`, `librc_sm.so`,
`libslice_sm.so`, `libtc_sm.so`). This is why plain `make install` is
avoided here — see `install_flexric_paths.sh`.

## 7. C xApps accept `-p`; the Python SDK does not

`swig_wrapper.cpp:55-57` calls `init_fr_args(1, NULL)`, so the Python
`ric.init()` always uses the compiled-in defaults and ignores any
arguments:

- service models: `/usr/local/lib/flexric/`
- config: `/usr/local/etc/flexric/flexric.conf`

The C xApps and the RIC itself do honour `-p <dir>` and `-c <file>`, so
only the Python path needs the root-owned directories. `sudo
./install_flexric_paths.sh` populates exactly those two locations.

## 8. RFSim reports null radio KPIs

Under RFSim there is no physical radio, so several E2SM-KPM measurements
the guide lists as available come back empty:

```
L1M.SS-RSRP    = 0 (null)
MR.NRScSSSINR  = 0 (null)
RRC.ConnMean   = 0 []
```

Only `CARR.PDSCHMCSDist` / `CARR.PUSCHMCSDist` carry real signal. This
bounds what any "add E2 RAN features" experiment can show on this testbed.

## Run order

```bash
# 1. Near-RT RIC (no root needed; -p avoids `make install`)
cd <flexric>/build/examples/ric
./nearRT-RIC -p ~/architecture_2/flexric_sm/ -c ~/architecture_2/flexric.conf

# 2. gNB with the E2 agent, then the UE
sudo ~/architecture_2/start_gnb_e2.sh
sudo ~/architecture_2/start_ue.sh

# 3. Steering daemon (root: drives tc)
sudo -E python3 ~/architecture_2/steer_daemon.py

# 4. ML xApp (normal user)
~/architecture_2/run_ml_xapp.sh
```

`sudo -E` matters in step 3: the daemon imports A1's controller, which
imports pandas from the user site-packages. Plain `sudo` runs with
`HOME=/root` and fails with `ModuleNotFoundError: No module named 'pandas'`.

Confirm the E2 association before expecting indications:

```bash
cat /proc/net/sctp/assocs     # expect a peer on port 36421
```

## Known operational quirk

The NTN RFSim UE loses sync roughly every 6 minutes (accumulated time drift
and frequency offset from the emulated LEO channel). It is expected, not a
fault. Long measurement runs must either checkpoint between repetitions or
use `~/ue_supervisor.sh`, which detects the drop and relaunches.
