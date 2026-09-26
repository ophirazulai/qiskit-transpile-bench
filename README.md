# qiskit-transpile-bench

A benchmark harness that answers one question about a change to Qiskit's transpiler:

> **Does the evolved Qiskit revision produce shallower circuits than the baseline, without
> breaking correctness, making other results worse, or compiling more slowly?**

You point it at two Qiskit source folders: a **baseline** (the reference) and an **evolved**
revision (your change). It builds each one in isolation, compiles a frozen set of benchmark
circuits with both, checks quality and, when the quality gate opens, semantic correctness
and cost, then returns a verdict: `PASS`, `NO_IMPROVEMENT`, `CONSTRAINT_VIOLATION`,
`INCONCLUSIVE` or `ERROR`.

The objective is **native two-qubit depth** (`D2`). Two-qubit gate count (`N2`), compile
time, memory and correctness are constraints that must not get worse.

## Requirements

Linux (macOS for local work), Python 3.11+, Git, [uv](https://docs.astral.sh/uv/), `rustup`
(with the baseline's Rust toolchain), a C/C++ compiler, and network access or warm caches for
the pinned Python and Rust dependencies ([setup and environments](docs/environments.md)).
On a cluster: LSF (`bsub`, `bjobs`, `bhist`, `bkill`) and a filesystem mounted at the same
path on every node.

## Start a session on LSF

Running through LSF is the main way to use this repository. From a login node:

```bash
uv sync --all-extras
mkdir -p /shared/qtb-store /shared/qtb-sessions
uv run python lsf/submit.py \
  --store /shared/qtb-store \
  --results-root /shared/qtb-sessions/idea1 \
  --baseline /shared/qiskit-baseline \
  --evolved /shared/qiskit-evolved \
  --cost-ncpus 56
```

The launcher validates everything, submits one manager job and prints its job ID, the log
directory and the ledger. The manager runs every stage as its own job, retries a cost
measurement that saw interference on its quiet cores, decides, and cleans the session.
`--results-root` is the exact directory of this session's data and must not exist yet. The
queue and hardware tier above are site values: see the **[LSF session guide](docs/cluster.md)**
before the first run.

To run the stages yourself (local work, debugging, replay), see [running the stages
directly](docs/workflow/README.md).

## Read the results

Open `report.md` in the session first, then `decision.json`; the LSF orchestration report
(`<results-root>.lsf/report.md`) shows every job, cost attempt and the retry budget.

| Verdict | Exit | Meaning |
| --- | ---: | --- |
| `PASS` | 0 | Quality improved and every required constraint passed |
| `NO_IMPROVEMENT` | 10 | Improvement rule failed; semantic correctness may not have run |
| `CONSTRAINT_VIOLATION` | 20 | A candidate check or cost guard failed |
| `INCONCLUSIVE` | 30 | Evidence or a required stage is missing/unresolved (including no clean cost measurement), or the baseline failed |
| `ERROR` | 40 | Harness, build or worker failure |

The manager job exits with the verdict's code. [decide](docs/workflow/decide.md) explains
each verdict and what to do next.

## Profiles

Iterate on the [iterations profile](docs/iterations-profile.md) (the default: three
100-qubit circuits, about 930 quality compiles per revision) and confirm once on the
[confirm profile](docs/confirm-profile.md) (133 cases, 8 families, levels 0–3), in a
separate session. Do not tune on the confirm profile: its workload is public and has no
held-back part.

## Documentation

The [documentation index](docs/README.md) starts with the [LSF session
guide](docs/cluster.md), then the [workflow overview](docs/workflow/README.md) and one page per
stage, the [session rules](docs/sessions.md), [baseline store](docs/store.md), [output
formats](docs/output-format.md), [metrics](docs/metrics.md), [architecture](docs/architecture.md),
[versioning](docs/versioning.md) and [validation evidence](docs/validation.md).
[`lsf/README.md`](lsf/README.md) maps the LSF modules for developers.

## Developing the harness

```bash
uv run pytest
```

```bash
uv run ruff check src worker verifier tests lsf
```

```bash
uv run python tools/probes/confirm_panel.py
```

`uv run pytest` runs `tests/` and `lsf/tests/`; the LSF tests use a fake scheduler and
synthetic counters and submit nothing. CI (`.github/workflows/ci.yml`) runs these on Python
3.11–3.13. An opt-in workflow (`controlled-runner.yml`) runs a confirm comparison on a
self-hosted controlled runner and archives the evidence.
