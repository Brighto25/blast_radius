#!/bin/bash
# Run every test (<= $1 s) unpatched through the selector's runner, source tree read-only.
T="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf ~/br/ro && cp -r ~/br/sqlglot ~/br/ro && rm -rf ~/br/ro/.git
find ~/br/ro -name __pycache__ -prune -exec rm -rf {} + ; ~/br/venv/bin/python -m compileall -q ~/br/ro >/dev/null
chmod -R a-w ~/br/ro
mkdir -p ~/br/emptydata/commits && cp "$T/kit/sample/tests.json" ~/br/emptydata/
cd ~/br/ro && HOME=/tmp BR_BASELINE=${1:-5} BR_REPO=~/br/ro BR_DATA=~/br/emptydata BR_MAP="$T/map.json.gz" PATH=~/br/venv/bin:$PATH \
  python "$T/selector.py" 2>&1 >/dev/null | grep -E "BASELINE|killed|status=" | cut -c1-3000
