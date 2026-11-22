#!/bin/bash
# Measure per-test CPU with in-process pools (run inside WSL / Linux).
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
cd ~/br/sqlglot
rm -rf ~/br/cpuout
export PATH=~/br/venv/bin:$PATH
export CPUOUT=~/br/cpuout SKIP_INTEGRATION=0 PYTHONPATH=.:"$TOOLS"
timeout 1200 python -m pytest -q -n 2 -p cpuplugin -p no:cacheprovider tests > ~/br/cpufull.txt 2>&1
echo "rc=$?"
tail -2 ~/br/cpufull.txt
