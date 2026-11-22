#!/bin/bash
# usage: check_one.sh <patch> <pytest node id>  -> does that test fail on the patched tree, and how fast
cd ~/br/lab && git checkout -q -- . && git apply "$1" || exit 1
export PATH=~/br/venv/bin:$PATH SKIP_INTEGRATION=0
/usr/bin/time -f "wall=%e" python -m pytest -q -x -p no:cacheprovider "$2" 2>&1 | tail -3
git checkout -q -- .
