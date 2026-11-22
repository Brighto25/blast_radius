#!/bin/bash
set -u
TOOLS="$(cd "$(dirname "$0")" && pwd)"
cd ~/br/sqlglot
export PATH=~/br/venv/bin:$PATH SKIP_INTEGRATION=0
python "$TOOLS/import_trace.py" "$TOOLS/../kit/sample/tests.json" ~/br/imp
CPUOUT=~/br/cpuout python "$TOOLS/build_map.py" ~/br/mapout ~/br/imp/import_trace.json ~/br/sqlglot "$TOOLS/../kit/sample/tests.json" "$TOOLS/../map.json.gz"
