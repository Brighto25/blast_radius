#!/bin/bash
# Trace exact executed sqlglot lines for every test (ALL_LINES) into ~/br/lineout.
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
cd ~/br/sqlglot
rm -rf ~/br/lineout
export PATH=~/br/venv/bin:$PATH
export ALL_LINES=1 MAPOUT=~/br/lineout SKIP_INTEGRATION=0 PYTHONPATH=.:"$TOOLS"
timeout 1500 python -m pytest -q -n 2 -p mapplugin -p no:cacheprovider tests > ~/br/linefull.txt 2>&1
echo "rc=$?"
tail -2 ~/br/linefull.txt
ls -la ~/br/lineout; du -sh ~/br/lineout
