# `lsf/`: LSF orchestration and the monitored cost job

User guide: [docs/cluster.md](../docs/cluster.md). This page is for developers.

The harness (`src/qtb`) executes one stage invocation and reports its outcome. Everything
specific to LSF lives here: no `LSB_*`/`LSF_*` parsing, LSF command or resource string is
left in `src/qtb`, and no retry policy either.

## Entry points

| Command | Runs where | Does |
| --- | --- | --- |
| `python lsf/submit.py` | login node | validates the configuration, writes `<session>.lsf/launch.json` and the ledger, submits the manager |
| `python -P -m lsf.manager --lsf-dir D` | manager job | the pipeline: stage jobs, cost retries, inline `decide`, the clean job |
| `python -P -m lsf.job --lsf-dir D --job-key K --stage S [--attempt N]` | each stage job | allocation → execution context, installs the monitor, runs one harness command, writes `outcomes/K.json` |
| `python -m lsf.control status\|stop\|reap --results-root S` | login node | watch, stop, recover |

## Modules

| Module | Responsibility | In the harness identity |
| --- | --- | --- |
| `context.py` | LSF environment → validated execution context; allocation checks | measurement-facing |
| `cost_monitor.py` | topology and placement, idle probe, counters, checks A and B, thresholds, contamination | measurement-facing |
| `cost_evidence.py` | admission of monitored bundles (reuse, stage, `decide`, replay after cleanup) | measurement-facing |
| `retry.py` | ledger, attempt accounting, `MAX_COST_RETRIES = 20`, exhaustion | yes |
| `scheduler.py` | `bsub`/`bjobs`/`bhist`/`bkill`, resource requests, response parsing | yes |
| `manager.py`, `job.py`, `submit.py`, `control.py`, `report.py`, `logging.py` | orchestration, entry points, reports, logs | yes |

The whole package (not `tests/`) is part of `implementation_identity()` and the archived
harness wheel, so a session pins it. The three measurement-facing modules also form
`measurement_identity()`, which every monitored bundle records and `run.json:cost_evidence`
freezes.

## Harness interfaces used

- `qtb.execution.Execution`: `scheduler`, `workers`, `invocation`, `session` (compile records
  `cost_evidence`), `cost_monitor`, `cleanup`.
- Worker lifecycle hooks in `qtb.coordinator.process.run_worker`: `spawn_options`,
  `launched`, `progress` (may return an exception: abort before commit), `exited` (before
  reaping, via `waitid(WNOWAIT)`), `reaped`.
- `qtb.errors.Contaminated`: ends a stage `noisy` with exit 42; never recorded as
  unresolved evidence.
- `qtb.extensions`: the session's required evidence validator, `lsf.cost_evidence:validate`.
- `qtb.coordinator.clean.after_decide(..., cleanup=...)`: the manager submits `clean` as a job.

## Tests

`lsf/tests/` runs with `uv run pytest`. A synthetic probe replaces `/proc` and `/sys` (with a
virtual clock), and `FakeScheduler` replaces LSF: it runs every job in-process through the
real `lsf.job` entry point over the fake stage work of `tests/stage_world.py`. Nothing is
submitted. `LinuxProbe` parsing is tested against a fake `/proc` tree; the real Linux path
(placement, `waitid`, counters) needs a Linux host.

## Formats

`launch.json` (`qtb-lsf-launch/1`), `ledger.json` (`qtb-lsf-ledger/1`), `outcomes/*.json`
(`qtb-lsf-outcome/1`), `report.json` (`qtb-lsf-report/1`), the execution context
(`qtb-lsf-context/1`), and monitoring evidence in cost bundles
(`qtb-lsf-monitor-evidence/1`, contract `qtb-lsf-monitor/1`). See
[output formats](../docs/output-format.md#lsf-orchestration-records) and
[versioning](../docs/versioning.md).
