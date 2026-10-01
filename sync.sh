#!/usr/bin/env bash
# sync.sh -- refresh this repo's copies from the live testbed directories.
#
# The live scripts hardcode ~/architecture_N paths, so they stay where they
# are and this repo holds copies. Run this, then commit:
#
#   ./sync.sh && git add -A && git commit -m "sync" && git push
#
# Scripts and configs only: no collected data, models or results.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
OAI_CONF="$HOME/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF"

copy_code() {   # $1 = source dir, $2 = destination in repo
    local src="$1" dst="$REPO/$2"
    [ -d "$src" ] || { echo "  skip: $src not found"; return; }
    mkdir -p "$dst"
    # rsync --delete inside the filter set only, so a script removed from the
    # testbed disappears here too.
    rsync -a --delete --prune-empty-dirs \
        --exclude='script_backups/' --exclude='__pycache__/' \
        --include='*/' \
        --include='*.py' --include='*.sh' --include='*.conf' --include='*.md' \
        --exclude='*' \
        "$src/" "$dst/"
    echo "  $2: $(find "$dst" -type f | wc -l) files"
}

echo "Syncing from live testbed:"
copy_code "$HOME/architecture_1" architecture_1
copy_code "$HOME/architecture_2" architecture_2

# The NTN gNB configs live inside the OAI source tree (untracked there).
# The .bak files are deliberately skipped.
mkdir -p "$REPO/oai_conf"
# Only the monolithic gNB configs (A1, A2); the split CU/DU configs are not
# part of this project.
for f in "$OAI_CONF"/gnb.sa.band254.u0.25prb.rfsim.ntn-leo*.conf \
         "$OAI_CONF"/gnb.sa.band78.fr1.106PRB.usrpb210.conf; do
    [ -f "$f" ] && cp "$f" "$REPO/oai_conf/"
done
echo "  oai_conf: $(ls "$REPO/oai_conf" | wc -l) files"
