#!/bin/bash
# Build, run once under the scorer's flags, print score and container CPU seconds (cgroup).
set -u
T="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="$T/kit/sample"
cd "$T"
docker build --platform linux/amd64 --tag blast-radius-entry . > ~/br/docker_build.log 2>&1 || { echo BUILD FAILED; tail -30 ~/br/docker_build.log; exit 1; }
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$SAMPLE/commits":/data/commits:ro \
  --volume "$SAMPLE/tests.json":/data/tests.json:ro \
  --entrypoint sh blast-radius-entry -c 'python /app/selector.py; grep usage_usec /sys/fs/cgroup/cpu.stat >&2' \
  > ~/br/docker_out.jsonl 2> ~/br/docker_err.log
grep usage_usec ~/br/docker_err.log
python3 "$T/kit/score.py" "$SAMPLE" "$SAMPLE/answers.json" ~/br/docker_out.jsonl | grep -E "Simul|Miss"
