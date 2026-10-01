#!/bin/bash
# The FlexRIC Python SDK cannot be told where the service-model libraries
# live: swig_wrapper.cpp:55-57 calls init_fr_args(1, NULL), so it always
# uses the compiled-in defaults below. The C xApps accept -p and -c, the
# Python ones do not. This installs just enough for the Python path.
#
# Deliberately NOT "make install": that would also drop
# libe42_xapp_shared.so into the service-model directory, and the Near-RT
# RIC dlopen()s every .so it finds there -- that library has an
# unresolved OAI RRC symbol and aborts the RIC on startup.
#
# Run as root:  sudo ~/architecture_2/install_flexric_paths.sh
set -eu

SRC=/home/bluelabuser/architecture_2
LIB_DIR=/usr/local/lib/flexric
ETC_DIR=/usr/local/etc/flexric

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root: sudo $0" >&2
  exit 1
fi

mkdir -p "$LIB_DIR" "$ETC_DIR"

# Only the 8 service models -- the same set the running RIC validated.
cp -v "$SRC"/flexric_sm/*.so "$LIB_DIR"/
cp -v "$SRC"/flexric.conf "$ETC_DIR"/flexric.conf

echo
echo "Installed service models:"
ls -1 "$LIB_DIR"
echo
echo "Config: $ETC_DIR/flexric.conf"
echo "Done. To undo: rm -rf $LIB_DIR $ETC_DIR"
