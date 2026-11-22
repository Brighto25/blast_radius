#!/bin/bash
# Same as the kit's `make sample`: build, run under the scorer's flags, score.
set -u
T="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="$T/kit/sample"
cd "$T"
if [ "${SKIP_BUILD:-0}" != 1 ]; then
  docker build --platform linux/amd64 --tag blast-radius-entry . > ~/br/docker_build.log 2>&1 || { echo BUILD FAILED; tail -30 ~/br/docker_build.log; exit 1; }
  echo "build ok"
fi
start=$(date +%s.%N)
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  ${EXTRA_ENV:-} \
  --volume "$SAMPLE/commits":/data/commits:ro \
  --volume "$SAMPLE/tests.json":/data/tests.json:ro \
  --name br-run blast-radius-entry > ~/br/docker_out.jsonl 2> ~/br/docker_err.log
echo "run rc=$? wall=$(echo "$(date +%s.%N) - $start" | bc)"
grep -vE "^\s|nanobind|leak|Unable to set|OPTIMIZE|^ - " ~/br/docker_err.log | tail -8
python3 "$T/kit/score.py" "$SAMPLE" "$SAMPLE/answers.json" ~/br/docker_out.jsonl
