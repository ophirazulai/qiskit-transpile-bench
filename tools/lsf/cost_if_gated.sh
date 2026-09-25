#!/usr/bin/env bash
# Run a session's cost stage only when it will measure something.
#
#   tools/lsf/cost_if_gated.sh SESSION COST_MODEL
#
# Runs as a light one-slot LSF job after correctness. When the quality gate is closed, or
# correctness found a failure, cost records "skipped" here within seconds. Otherwise cost is
# submitted on an exclusive host (-x) of the model the fixed cost thresholds were set on, and
# this job waits for it (-K) and exits with its status. bsub -K also returns non-zero when
# the submission itself fails, so this job never ends before the cost job has ended.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    sed -n '2,10p' "$0" >&2
    exit 64
fi
R=$1
COST_MODEL=$2
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(cd "$HERE/../.." && pwd)
Q="uv run --project $PROJECT qiskit-transpile-bench"

REASON=$(uv run --project "$PROJECT" python -c '
import sys
from qtb.coordinator.stages import skip_reason
print(skip_reason(sys.argv[1], "cost") or "")
' "$R")

if [[ -n "$REASON" ]]; then
    echo "cost will be skipped: $REASON"
    exec $Q cost --results-root "$R"
fi

set +e
bsub -K -x -n 1 -R "select[model==$COST_MODEL]" -J "kx-$(basename "$R")" \
     -o "$(dirname "$R")/lsf/$(basename "$R").cost.%J.out" \
     $Q cost --results-root "$R"
STATUS=$?
set -e
echo "cost job ended with status $STATUS"
exit $STATUS
