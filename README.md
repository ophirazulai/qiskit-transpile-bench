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

## Quick start

```bash
uv sync --all-extras
```

```bash
mkdir -p "$HOME/qtb-store"             # the baseline store; it must exist before compile
export QTB_STORE="$HOME/qtb-store"      # or pass --store DIR to compile
S="$HOME/qtb-sessions/idea1"            # choose a session path that does not exist yet

uv run qiskit-transpile-bench compile \
  --baseline /path/to/baseline --evolved /path/to/evolved --results-root "$S"
uv run qiskit-transpile-bench quality     --results-root "$S"
uv run qiskit-transpile-bench correctness --results-root "$S"      # skipped unless the quality gate is open
uv run qiskit-transpile-bench unit-tests  --results-root "$S"      # optional; skipped unless the gate is open
uv run qiskit-transpile-bench cost        --results-root "$S"      # skipped if correctness found a failure
uv run qiskit-transpile-bench decide      --results-root "$S"      # writes the verdict; exits with the verdict code
uv run qiskit-transpile-bench clean       --results-root "$S"      # optional: deletes the bulk, keeps the results
```

Add `--profile confirm-profile` to `compile` for the confirm profile. The later stages read
the profile and the store from the session. Replace the paths with your two Qiskit
checkouts. Your source folders are never modified. `uv run` runs the CLI in this project's
uv environment.

Each stage exits 0 when it completed or was skipped, so the stages up to `decide` can be
joined with `&&`. `correctness` and `unit-tests` can run at the same time, in two shells or
on two hosts.

**Requirements:** Linux or macOS, Python 3.11+, Git, [uv](https://docs.astral.sh/uv/),
`rustup` (with the baseline's Rust toolchain), a C/C++ compiler, and network access or warm
caches for the pinned Python and Rust dependencies. See [setup and
environments](docs/environments.md).

## Workflow

Each command runs one step against one session directory. The baseline store is shared
across sessions; the evolved build and measurements belong to one session. A finished stage
never runs again; an interrupted or failed one resumes when called again.

| Step | Command guide | Purpose |
| --- | --- | --- |
| 1 | [compile](docs/workflow/compile.md) | Snapshot sources, reuse/build the baseline, build the evolved revision and verifier, round-trip inputs |
| 2 | [quality](docs/workflow/quality.md) | Measure circuit depth/count, check outputs, audit determinism and compute the gate |
| 3 | [correctness](docs/workflow/correctness.md) | C1–C5, API contracts and C7; baseline preflight first |
| 4 | [unit-tests](docs/workflow/unit-tests.md) | Optional upstream Python/Rust suites; once started they bind the verdict |
| 5 | [cost](docs/workflow/cost.md) | Fresh timing and memory panels on a quiet, exclusive host |
| 6 | [decide](docs/workflow/decide.md) | Merge committed evidence into `decision.json` and `report.md` |
| 7 | [clean](docs/workflow/clean.md) | Remove this session's bulk while keeping results; the store and original sources are untouched |

The flow is `compile → quality → gate`. An improvement with every quality check passing
opens the gate; identical builds also proceed as an A/A harness check. Otherwise later
measurement stages record `skipped`. **A closed-gate `NO_IMPROVEMENT` does not establish
semantic correctness or cost safety.** See [the
gate](docs/workflow/quality.md#the-quality-gate).

`correctness` and optional `unit-tests` can run in parallel. `cost` waits for correctness to
finish without a failed record. `decide` can run any time after the session exists;
unfinished required stages keep the verdict `INCONCLUSIVE`, and a stage crash gives `ERROR`.

## The two profiles

A profile is a frozen workload plus its thresholds. **Iterate on the iterations profile and
confirm once on the confirm profile.**

| | [iterations-profile](docs/iterations-profile.md) (default) | [confirm-profile](docs/confirm-profile.md) |
| --- | --- | --- |
| Purpose | Fast inner loop | One-time broad check before you claim a gain |
| Scored workload | 3 circuits of 100 qubits (QFT, Heisenberg, QAOA), level 2, heavy-hex `cz` | 133 cases: 38 input groups, 8 circuit families, levels 0–3, 11 targets |
| Guards | Same circuits on `cx` and `ecr`, 3 canaries, 19 timing cases, 3 preset cases | 19 guards, 48 deterministic, 4 zero-baseline, 5 canaries, 53 timing, 9 memory |
| Improvement rule | `D2` lower by more than 2 standard errors | `D2` at least 1% lower beyond 2 standard errors, and improved in at least 4 of 8 families |
| Quality compiles per revision | About 930 | About 15,000 |

Do not tune on the confirm profile. Its workload is public and has no held-back part. The
confirm report also shows the score with the three iterations circuits removed.

## Read the results

Open `report.md` in the session first. It lists stage states, skipped reasons, quality
estimates, constraints needing attention and baseline reuse.

| Verdict | Exit | Meaning |
| --- | ---: | --- |
| `PASS` | 0 | Quality improved and every required constraint passed |
| `NO_IMPROVEMENT` | 10 | Improvement rule failed; semantic correctness may not have run |
| `CONSTRAINT_VIOLATION` | 20 | A candidate check or cost guard failed |
| `INCONCLUSIVE` | 30 | Evidence or a required stage is missing/unresolved, or the baseline failed |
| `ERROR` | 40 | Harness, build or worker failure |

These are `decide` exit codes. Measurement stages exit 0 for usable completed or skipped
work, 40 for a crash, 41 for an unmet precondition and 64 for usage errors. [Session
rules](docs/sessions.md) and [decide](docs/workflow/decide.md) explain the details.

## Documentation

The [documentation index](docs/README.md) separates workflow guides from references:

- [Setup and environments](docs/environments.md), [sessions](docs/sessions.md),
  [baseline store](docs/store.md) and [cluster execution](docs/cluster.md).
- [Metrics](docs/metrics.md), [verifier contracts](docs/verifier.md),
  [output formats](docs/output-format.md), [architecture](docs/architecture.md) and
  [versioning](docs/versioning.md).
- [Implementation and runner validation](docs/validation.md), with dated evidence and
  remaining known-outcome validation work.

## Developing the harness

```bash
uv run pytest
```

```bash
uv run ruff check src worker verifier tests
```

```bash
uv run python tools/probes/confirm_panel.py
```

CI (`.github/workflows/ci.yml`) runs these on Python 3.11–3.13. An opt-in workflow
(`controlled-runner.yml`) runs a confirm comparison on a self-hosted controlled runner and
archives the evidence.
