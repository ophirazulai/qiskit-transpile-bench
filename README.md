# qiskit-transpile-bench

A benchmark harness that answers one question about a change to Qiskit's transpiler:

> **Does the evolved Qiskit revision produce shallower circuits than the baseline, without
> breaking correctness, making other results worse, or compiling more slowly?**

You point it at two Qiskit source folders: a **baseline** (the reference) and an **evolved**
revision (your change). It builds each one in isolation, compiles a frozen set of benchmark
circuits with both, checks every output, and returns a verdict: `PASS`, `NO_IMPROVEMENT`,
`CONSTRAINT_VIOLATION`, `INCONCLUSIVE` or `ERROR`.

The objective is **native two-qubit depth** (`D2`). Two-qubit gate count (`N2`), compile time,
memory and correctness are constraints that must not get worse.

## Quick start

```bash
uv sync --all-extras
```

```bash
uv run qiskit-transpile-bench smoke --baseline /path/to/baseline --evolved /path/to/evolved
```

```bash
uv run qiskit-transpile-bench compare --baseline /path/to/baseline --evolved /path/to/evolved
```

```bash
uv run qiskit-transpile-bench compare --baseline /path/to/baseline --evolved /path/to/evolved --profile confirm-profile
```

Replace the paths with your two Qiskit checkouts. Your source folders are never modified.
`uv run` runs the CLI in this project's uv environment.

**Requirements:** Linux or macOS, Python 3.11+, Git, [uv](https://docs.astral.sh/uv/),
`rustup` (with the baseline's Rust toolchain), a C/C++ compiler, and network access or warm
caches for the pinned Python and Rust dependencies. See [environments](docs/environments.md).

## How it works, in plain words

1. **Copy and build.** Each source folder is snapshotted and built from scratch into its own
   virtual environment as a release wheel, with the same pinned dependencies and the
   baseline's Rust toolchain. `compare` also builds the baseline a second time as a noise
   control. A separate environment gets an independently pinned Qiskit 2.5.2 for checking
   results.
2. **Same inputs.** Both revisions rebuild every benchmark circuit and target from frozen data
   and must reproduce the exact hashes, which proves they compile the same thing.
3. **Correctness first.** Both revisions run a frozen suite of 2,400 small compiles that
   are checked exactly, plus Clifford checks on the large circuits and the baseline's own
   upstream Python and Rust tests.
4. **Compile the workload.** Every benchmark case is compiled with up to 100 transpiler seeds by
   both revisions. Every output is checked for legality on the target, and routing is
   replayed exactly to prove the layout and SWAPs are right.
5. **Score.** The harness computes `D2` and `N2` itself from the exported circuits (the
   candidate never grades itself), then compares the two revisions seed by seed.
   An improvement counts only if it is larger than two standard errors of the seed noise.
6. **Time it.** If depth improved and nothing failed, compile time (and memory, for confirm)
   is measured on the baseline, the control build and the candidate, interleaved on a quiet
   machine.
7. **Decide.** Every check becomes a record. The verdict follows fixed rules, and
   `report.md` explains it.

More detail: [architecture](docs/architecture.md) and [metrics](docs/metrics.md).

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

Do not tune on the confirm profile. Its workload is public and has no held-back part. Every
report shows how many decisions already exist for the profile, and the confirm report also
shows the score with the three iterations circuits removed.

## The smoke test

`smoke` is an optional quick check that both revisions build and run. It does not produce a
verdict.

It builds the baseline and evolved release wheels (no control build) and round-trips the
quality cases. It then compiles each non-timing, non-memory case with seed 0 only for both
revisions, and runs only the structural legality and metrics check on each output. It skips
the correctness suites, calibration, routing replay, semantic oracles, the quality cache and
cost measurement.

`compare` does not depend on a smoke run and covers everything smoke does. Use smoke on a new
evolved tree or build environment, or during fast iteration, to catch build, toolchain or
worker failures early instead of partway through a full comparison. A passing smoke run is not
qualification and says nothing about improvement. It writes `smoke.json` and exits with 0
(success) or 40 (failure).

## Reading the verdict

`compare` prints the verdict and the run directory. It writes `decision.json` (for machines)
and `report.md` (for people) into `results/runs/<run>/`. See
[output format](docs/output-format.md).

| Verdict | Exit | Meaning | What to do next |
| --- | ---: | --- | --- |
| `PASS` | 0 | Depth improved and every required constraint passed | On iterations: run confirm. On confirm: you have a claim for this workload |
| `NO_IMPROVEMENT` | 10 | Clean, complete comparison, but no gain beyond seed noise | Keep iterating |
| `CONSTRAINT_VIOLATION` | 20 | The candidate failed a correctness check, or a quality/cost guard showed a real regression | Read "Constraints needing attention" in `report.md` |
| `INCONCLUSIVE` | 30 | Something required is missing or unresolved, the baseline itself failed, or the setup is unqualified | Read the listed reasons; this is also the expected result for unqualified setups |
| `ERROR` | 40 | The harness, a build or an input failed | Check the build or worker logs named in the error |

Usage errors exit 64.

**Qualification is still required for `PASS`.** This repository ships executable profiles and
harness validation, not a qualified measurement setup. Every run requires a
`harness/qualification` record, which exists only after a maintainer has validated the setup on
a controlled runner and written an attestation file. Until then, even a fully successful run
ends `INCONCLUSIVE`. The baseline's upstream Python and Rust tests must also pass. See
[implementation status](docs/implementation-status.md).

Changes to the optimization stage, two-qubit synthesis or shared infrastructure are
not verified at full scale by any check, so on the iterations profile they reach at most
`INCONCLUSIVE`. Layout and routing changes are fully covered. See
[stage coverage](docs/metrics.md#7-change-scope-and-stage-coverage).

## Commands and options

| Command | What it does |
| --- | --- |
| `compare` | Full comparison and verdict |
| `smoke` | Build and one-seed structural check; no verdict |
| `evaluate --run RUN_DIRECTORY` | Recompute the verdict of a finished run from its saved data, without compiling. Writes into `RUN_DIRECTORY/reevaluations/` |

Options for `compare` and `smoke`:

| Option | Meaning |
| --- | --- |
| `--baseline PATH`, `--evolved PATH` | The two Qiskit source folders (required) |
| `--profile NAME` | `iterations-profile` (default) or `confirm-profile` |
| `--results-root DIR` | Where runs, caches and calibrations go (default `results/`) |
| `--resume RUN_DIRECTORY` | Continue an interrupted run with its archived snapshots, manifest and policy. Requires the same harness version; install the archived harness wheel to resume an older run |
| `--change-scope scope.json` | Declare extra changed stages, for example `{"stages": ["optimization"]}`. This can only widen the scope inferred from the changed files; unknown paths already count as all stages |

## How long it takes

Measured on an Apple M1 Max laptop. See [environments](docs/environments.md#how-long-it-takes)
for details.

| Step | Time |
| --- | --- |
| Building one Qiskit revision from source | 20–60 min (24 and 52 min measured). Cached for unchanged sources |
| Whole `smoke` run, first time | About 1 h 20 min, almost all of it building |
| Iterations quality compiles | About 10 CPU-minutes per revision, roughly doubled by routing replay |
| Confirm quality compiles | About 3.5 CPU-hours for the candidate once the baseline is cached |
| Cost panels, iterations | One round (3 arms × 16 cases) is about 2–2.5 min. Typical: the 4-round screen, about 10 min. Worst: screen + 6-round full measurement + 12-round rerun = 22 rounds, about 50 min. Add about 10–20 min for the multi-seed companion when the change touches layout or routing |
| Cost panels, confirm | One round (3 arms × 36 cases) is about 5 min. Typical: the 4-round screen (about 20 min) plus memory (about 15 min), about 35–40 min. Worst: screen + 10-round full + 20-round rerun = 34 rounds, about 3 h, plus a memory rerun |
| First `compare` against a new baseline | Also pays for calibration and upstream tests (budgets of 4 h Python and 3 h Rust); calibration is reused for 30 days. Calibration compiles the baseline on two extra seed blocks (about 25 CPU-min for iterations, about 3 CPU-h for confirm) and times baseline against control for 30 rounds on every cost panel: about 1 h for the batched timing panels, plus the multi-seed companion at 30 rounds per seed (about 2–6 h for iterations, more for confirm), which dominates |

Cost panels are estimates from the design probes, not measured runs: no full comparison with
cost measurement has been recorded yet. They run only after the quality test has passed, on an
otherwise idle machine.

**Why the cost panels are this fast.** Each panel starts with a 4-round *screen*. If the
candidate's compile time already sits inside the noise band of the full measurement (and no
single case is more than 10% slower), the panel stops there. Otherwise it is measured in full
(6 rounds in the iterations profile, 10 in confirm) and, on a candidate-only breach, once more
with doubled rounds. Every round is one fresh process per arm that times the whole panel, so
Python and Qiskit start-up is paid 3 times per round instead of once per case. The `cx`/`ecr`
twins of the scored circuits are timed only when the change can reach translation or
optimization, since they repeat the `cz` cases' layout and routing. See
[iterations-profile.md](docs/iterations-profile.md#timing-panels) and
[metrics.md](docs/metrics.md#6-cost-time-and-memory).

Disk: about 2 GB per built revision, 5–7 GB per run. Run directories are never deleted
automatically.

## Reproducibility guarantees

- Every quality panel uses seeds 0–99 (block B0). Calibration uses separate baseline blocks
  100–199 and 200–299.
- Workers run serially (`QISKIT_PARALLEL=FALSE`, one Rust and BLAS thread, `PYTHONHASHSEED=0`),
  and a determinism audit recompiles a sample to prove outputs are reproducible.
- Quality results are cached by build, case definition, machine, harness and measurement
  protocol, so a rerun against the same baseline only compiles the candidate.
- Cost samples belong to one run and are never reused in another comparison.
- Profiles and fixtures are frozen and hashed. No comparison regenerates its inputs.
  Six inputs whose redistribution terms were unclear were replaced by recorded Qiskit
  constructions; see [fixture provenance](fixtures/PROVENANCE.md).
- A changed fixture, weight, threshold or canary value requires a new profile version. See
  [versioning](docs/versioning.md).

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](docs/architecture.md) | Components, trust boundaries, process model, run lifecycle, caches, source layout |
| [Iterations profile](docs/iterations-profile.md) | The default workload, acceptance rules IA1–IA6, statistical power, budget |
| [Confirm profile](docs/confirm-profile.md) | The broad workload, families and weights, rules CA1–CA6, report-only numbers |
| [Metrics](docs/metrics.md) | `D2`/`N2`, scores and paired standard errors, guards, cost estimators, calibration, stage coverage, verdict |
| [Verifier](docs/verifier.md) | The pinned semantic oracle and every correctness check (C0–C7, C1-lite, upstream tests) |
| [Environments](docs/environments.md) | `envs/` lock files, how builds work, where they live, build time and disk use, troubleshooting |
| [Output format](docs/output-format.md) | Run directory, `decision.json`, `report.md`, `smoke.json`, observations, circuit files |
| [Implementation status](docs/implementation-status.md) | What is implemented and validated, and what qualification still needs |
| [Versioning](docs/versioning.md) | Format, profile and compatibility rules |
| `design/transpilation-benchmark-impl-plan.md` | The full design and its reasoning |

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
