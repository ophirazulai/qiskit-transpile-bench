#!/usr/bin/env bash
# Submit one session's whole stage chain to LSF. An example, not harness code: adapt the
# queues, slot counts and walltimes (-W, see the README's timing table) to your site.
#
#   tools/lsf/submit.sh NAME BASELINE EVOLVED COST_MODEL [PROFILE] [--no-unit-tests]
#
# The session directory and the store must be on a filesystem mounted at the same path on
# every node, and so must the uv Python that builds the environments (docs/environments.md).
set -euo pipefail

if [[ $# -lt 4 ]]; then
    sed -n '2,9p' "$0" >&2
    exit 64
fi
NAME=$1
B=$(realpath "$2")
E=$(realpath "$3")
COST_MODEL=$4
PROFILE=${5:-iterations-profile}
UNIT_TESTS=1
[[ "${6:-}" == "--no-unit-tests" ]] && UNIT_TESTS=0

HERE=$(cd "$(dirname "$0")" && pwd)
Q="uv run --project $(cd "$HERE/../.." && pwd) qiskit-transpile-bench"
STORE=${QTB_STORE:-/shared/qtb-store}          # one per site; holds the baselines
SESSIONS=${QTB_SESSIONS:-/shared/qtb-sessions}
R=$SESSIONS/$NAME                             # this session; must not exist yet
LOGS=$SESSIONS/lsf
mkdir -p "$LOGS"
O="-o $LOGS/$NAME.%J.out"

if [[ -e "$R" ]]; then
    echo "Session $R already exists; choose another NAME" >&2
    exit 64
fi

# -ti ends a job whose dependency can never be met; without it, a job would stay pending
# forever and decide, which waits on ended(), would never start.
bsub -J "b-$NAME" -n 16 $O $Q compile --baseline "$B" --evolved "$E" \
     --store "$STORE" --profile "$PROFILE" --results-root "$R"
bsub -J "q-$NAME" -n 12 -ti -w "done(b-$NAME)" $O $Q quality --results-root "$R"
bsub -J "c-$NAME" -n 12 -ti -w "done(q-$NAME)" $O $Q correctness --results-root "$R"
WAIT="ended(b-$NAME) && ended(q-$NAME) && ended(c-$NAME) && ended(k-$NAME)"
if [[ $UNIT_TESTS == 1 ]]; then
    bsub -J "u-$NAME" -n 8 -ti -w "done(q-$NAME)" $O $Q unit-tests --results-root "$R"
    WAIT="$WAIT && ended(u-$NAME)"
fi
# A one-slot job that records "skipped" locally, or runs cost on an exclusive host.
bsub -J "k-$NAME" -n 1 -ti -w "done(c-$NAME)" $O \
     "$HERE/cost_if_gated.sh" "$R" "$COST_MODEL"
bsub -J "d-$NAME" -n 1 -w "$WAIT" $O $Q decide --results-root "$R"
echo "Submitted $NAME: session $R, logs $LOGS"
