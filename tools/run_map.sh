#!/bin/bash
# Build the raw coverage traces (run inside WSL / Linux).
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
cd ~/br/sqlglot
rm -rf ~/br/mapout
export PATH=~/br/venv/bin:$PATH
python3 "$TOOLS/flag_lines.py" ~/br/sqlglot ~/br/flag_lines.json
export FLAG_LINES=~/br/flag_lines.json HEAVY_IDS="$TOOLS/heavy_ids.json" MAPOUT=~/br/mapout SKIP_INTEGRATION=0 PYTHONPATH=.:"$TOOLS"
timeout 1200 python -m pytest -q -n 2 -p mapplugin -p no:cacheprovider tests > ~/br/mapfull.txt 2>&1
echo "rc=$?"
tail -4 ~/br/mapfull.txt
ls -la ~/br/mapout
