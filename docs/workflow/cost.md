# 5. cost

[Documentation index](../README.md) · [← unit-tests](unit-tests.md) · [decide →](decide.md)

Measure timing and memory regressions on a quiet, exclusive host.

## Run this step

```bash
uv run qiskit-transpile-bench cost --results-root "$S"
```

`S` is the session directory chosen at `compile`. `correctness` must be complete or
gate-skipped. The quality gate must be open and correctness must have no failed record for
measurements to run. See [session rules](../sessions.md) for prerequisites, retries and
stage exit codes.

## Choose the host

Timing and memory must be measured on an otherwise idle host. The stage takes an exclusive
per-user runner lock and waits up to five minutes for the one-minute load average to fall
below half the core count. If it does not, timing evidence is unresolved.

On LSF, request an exclusive host with `bsub -x` and pin the model used for your fixed cost
thresholds. See [cluster execution](../cluster.md). Build OS, architecture and Python must
match this host, and build interpreters must be available at the saved absolute paths.

## What is measured

Both baseline and evolved arms are measured anew, interleaved in random order. Cost samples
never come from the store. Each timing round uses fresh processes; the quality compile times
are not used here.

| Panel | Purpose | Selection |
| --- | --- | --- |
| Timing | End-to-end `transpile()` and reused-pass-manager compiles | Always |
| Timing basis | `cx`/`ecr` twins of scored cases | Translation, optimization or unknown change scope |
| Preset | Pass-manager construction | Measured always; binds only for relevant or unknown scope |
| Confirm timing | Additional confirm circuits | Confirm profile |
| Memory | Peak RSS from fresh processes | Confirm profile |
| Companion | Average timing over seeds 0–19 | Layout, routing or unknown change scope |

[Profiles](../README.md#profiles) define the case membership;
[metrics](../metrics.md#6-cost-time-and-memory) defines every estimator, threshold and
regime. The change scope is inferred from the snapshot diff; it cannot be supplied by hand.

## Screen, full measurement and rerun

A timing panel first gets a four-round screen. If it clears half the panel noise band with
no per-case breach, it stops there. Otherwise the panel gets a full measurement: six rounds
on iterations or ten on confirm. A candidate-only breach triggers one fresh rerun with twice
the full count. A repeat breach is `failed`; a clean rerun is `passed_on_rerun`. Companion
and memory panels have no screen and use their own counts.

The fixed thresholds are `panel_ratio = 1.03`, `case_ratio = 1.10`, a 25 ms per-case timing
floor and a 32 MiB memory floor. There is no automatic calibration. Use an [A/A validation
session](../validation.md#known-outcome-controls) to check runner drift.

## Outputs and retries

Raw bundles live under `cost/`; the stage writes cost records under `stages/cost/`. Bundles
record the cost host, independent arm IDs, the protocol, thresholds and interleaving seed. A
retry reuses only bundles that pass validation for this session and regime. `decide` replays
complete cost bundles using the archived policy.

## Expected duration

These are design-probe estimates, not a measured full comparison:

| Profile | Typical panels | Worst timing path |
| --- | --- | --- |
| Iterations | Screen: about 6–7 min; add 10–20 min companion for layout/routing | 22 rounds (screen + full + rerun), about 35 min |
| Confirm | Screen about 14 min plus memory about 10 min | 34 timing rounds, about 2 h, plus a possible memory rerun |

If correctness found a failure, the stage records `skipped` before checking the host or
requiring quietness. A cost breach is a finding: the stage completes with exit 0. Run
[decide](decide.md) to turn the committed evidence into a verdict.
