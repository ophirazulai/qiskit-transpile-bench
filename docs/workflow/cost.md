# 5. cost

[Documentation index](../README.md) · [← unit-tests](unit-tests.md) · [decide →](decide.md)

Measure timing and memory regressions on quiet cores.

## Run this step

On LSF the manager submits this stage as the quiet cost job; you do not run it yourself
([LSF session guide](../cluster.md)). Directly, for a session created without LSF:

```bash
uv run qiskit-transpile-bench cost --results-root "$S"
```

`S` is the session directory chosen at `compile`. `correctness` must be complete or
gate-skipped. The quality gate must be open and correctness must have no failed record for
measurements to run; otherwise the stage records `skipped` before checking the host. See
[session rules](../sessions.md) for prerequisites, retries and stage exit codes.

## Measurement modes

Every bundle records its `measurement_mode`.

**Monitored cores (`cores`), on LSF.** The cost job holds nine exclusive physical cores on
one host of the approved hardware tier (`-n 9` with
`affinity[core(1,exclusive=(core,alljobs))]`, never `bsub -x`). Before measuring, the
monitor under `lsf/` checks the allocation (one host, nine slots, the exclusive-core request,
the tier selectors, the physical core count), reads the CPU topology, pins the coordinator
and itself to the first core and the serial worker to the second (both with their SMT
siblings), verifies the masks, and probes the idle worker core. The other seven cores stay
reserved. An allocation it cannot vouch for, unreadable topology or unavailable counters
refuse the stage (exit 41, nothing written).

During measurement it observes every actual worker process, including the fresh process
after a per-entry timeout, from launch to exit in bounded windows (2 s), and applies two
checks to every window.

Workers acknowledge a start and end boundary around each entry's measured calls, after
setup and warmup. These boundaries split monitoring windows, so setup cannot dilute a
short measurement's interference. Every entry must have a complete measured interval in
the saved evidence; replay refuses missing boundaries. The boundary handshakes run outside
the worker's timers and must be included in cluster overhead calibration.

| Check | Measured | Accepted when |
| --- | --- | --- |
| A. Foreign CPU activity | Busy time of the worker core, all SMT siblings, less the CPU time of the worker and its descendants over the same window | At most 5 % of physical-core time (at least 0.03 s for short windows) |
| B. Involuntary preemption | The worker's involuntary context switches over the same window, all threads and live descendants; the final window is completed from the reaped worker's usage | At most 4 per second (at least 2 for short windows) |

The first failing window stops and reaps the worker, keeps its diagnostics
(`cost/monitor/<job key>/contamination*.json`, and `contaminated.json` in the interrupted
regime's session directory) and ends the stage `noisy` (exit 42). Nothing measured by that
invocation is committed; complete regime bundles from earlier invocations stay valid. A
missing sample, an invalid counter or a moved CPU mask is never clean: it fails the stage
(exit 40). The machine-wide runner lock and the host-load wait do not apply in this mode:
they describe the whole host, not the allocated cores.

The checks detect CPU interference; they do not prove the absence of shared-cache,
memory-bandwidth, frequency or thermal effects. The paired, interleaved protocol below
remains the defense against those. The thresholds are starting points frozen with contract
`qtb-lsf-monitor/1` and must be calibrated on the chosen tier
([validation](../validation.md#monitored-cost-on-lsf)).

**Machine (`machine`), direct runs.** Without a monitor the stage takes an exclusive per-user
runner lock and waits up to five minutes for the one-minute load average to fall below half
the core count. If it does not, timing evidence is unresolved. Use an otherwise idle
machine.

A session launched on LSF records the monitoring contract in `run.json:cost_evidence`. Its
cost stage refuses to run without the monitor (exit 41), and every bundle must pass the
evidence validator when it is reused, when the stage judges it and whenever `decide`
replays it, also after `clean`. A bundle without admissible monitoring evidence counts as
unresolved, never as a pass. Build OS, architecture and Python must match the cost host,
and build interpreters must be available at the saved absolute paths.

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
record the cost host, independent arm IDs, the protocol, thresholds, the interleaving seed,
the measurement mode, every worker job and, in cores mode, the monitoring evidence: the
attempt, allocation, CPU layout, tier, thresholds, idle probe and every window's counters
([output formats](../output-format.md#cost-bundles)). A retry reuses only complete bundles
that pass validation for this session and regime; both arms of a bundle always come from
one host and one invocation. A valid failed comparison is never measured again: its saved
bundles decide. `decide` replays complete cost bundles using the archived policy.

## Expected duration

These are design-probe estimates, not a measured full comparison:

| Profile | Typical panels | Worst timing path |
| --- | --- | --- |
| Iterations | Screen: about 6–7 min; add 10–20 min companion for layout/routing | 22 rounds (screen + full + rerun), about 35 min |
| Confirm | Screen about 14 min plus memory about 10 min | 34 timing rounds, about 2 h, plus a possible memory rerun |

Monitoring aborts a contaminated invocation at its first bad window, usually within
seconds or minutes. If correctness found a failure, the stage records `skipped` before
checking the host or requiring quietness. A cost breach is a finding: the stage completes
with exit 0. Run [decide](decide.md) to turn the committed evidence into a verdict.
