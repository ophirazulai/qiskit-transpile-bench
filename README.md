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
mkdir -p "$HOME/qtb-store"             # the baseline store; it must exist before compile
export QTB_STORE="$HOME/qtb-store"      # or pass --store DIR to compile
Q="uv run qiskit-transpile-bench"
S="$HOME/qtb-sessions/idea1"            # the session directory: one baseline/evolved pair

$Q compile     --baseline /path/to/baseline --evolved /path/to/evolved --results-root "$S"
$Q quality     --results-root "$S"
$Q correctness --results-root "$S"      # skipped unless the quality gate is open
$Q unit-tests  --results-root "$S"      # optional; skipped unless the gate is open
$Q cost        --results-root "$S"      # skipped unless correctness also passed
$Q decide      --results-root "$S"      # writes the verdict; exits with the verdict code
$Q clean       --results-root "$S"      # optional: deletes the bulk, keeps the results
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
caches for the pinned Python and Rust dependencies. See [environments](docs/environments.md).

## How it works, in plain words

1. **Copy and build (`compile`).** Each source folder is snapshotted and built from scratch
   into its own virtual environment as a release wheel, with the same pinned dependencies
   and the baseline's Rust toolchain. The baseline build is kept in the **store** and reused
   by every later session with the same baseline. A separate environment gets an
   independently pinned Qiskit 2.5.2 for checking results.
2. **Same inputs (`compile`).** Both revisions rebuild every benchmark circuit and target
   from frozen data and must reproduce the exact hashes, which proves they compile the same
   thing.
3. **Compile the workload (`quality`).** Every benchmark case is compiled with up to 100
   transpiler seeds by both revisions. Every output is checked for legality on the target,
   and routing is replayed exactly to prove the layout and SWAPs are right. A sample is
   recompiled to prove the outputs are reproducible.
4. **Score and gate (`quality`).** The harness computes `D2` and `N2` itself from the
   exported circuits (the candidate never grades itself), then compares the two revisions
   seed by seed. An improvement counts only if it is larger than two standard errors of the
   seed noise. Without an improvement, or when a quality check did not pass, the quality
   gate closes and the session stops here.
5. **Correctness (`correctness`, and optionally `unit-tests`).** Both revisions run a frozen
   suite of 2,400 small compiles that are checked exactly, plus Clifford checks on the large
   circuits. The optional `unit-tests` stage runs the baseline's own upstream Python and
   Rust tests on both builds.
6. **Time it (`cost`).** If correctness found no failure, compile time (and memory, for
   confirm) is measured on the baseline and the candidate, interleaved on a quiet machine.
7. **Decide (`decide`).** Every check becomes a record. The verdict follows fixed rules, and
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

Do not tune on the confirm profile. Its workload is public and has no held-back part. The
confirm report also shows the score with the three iterations circuits removed.

## Commands

Each command does one stage against one session directory, given by `--results-root`.

| # | Command | What it does | Runs when | Node |
| ---: | --- | --- | --- | --- |
| 1 | `compile` | Creates the session, snapshots both sources, takes the baseline build from the store (or builds it into the store), builds the evolved revision and the verifier, and round-trips the inputs | Always | Busy |
| 2 | `quality` | Quality compiles, C0 legality, C6 routing replay, C1-lite (confirm), the determinism audit, the quality checks and the gate | After `compile` | Busy |
| 3 | `correctness` | The C1–C5 suite, API contracts and C7 Clifford checks: the baseline half first (or from the store), then the evolved half | Gate open | Busy |
| 4 | `unit-tests` | *Optional.* The baseline's upstream Python and Rust tests on both builds, plus the evolved tree's own Python tests (report-only) | Gate open | Busy |
| 5 | `cost` | Timing and memory panels | Gate open and `correctness` found no failure | Quiet, exclusive |
| 6 | `decide` | Merges the evidence of the finished stages and writes the verdict and report | Any time after `compile` created the session; as often as you like | Any |
| 7 | `clean` | Deletes the session's bulk and keeps its results | After `decide` | Any |

A stage needs the one before it to be complete or skipped: `quality` needs `compile`,
`correctness` and `unit-tests` need `quality`, and `cost` needs `correctness`. `unit-tests`
is independent of `correctness` and `cost`.

Options:

| Option | Command | Meaning |
| --- | --- | --- |
| `--baseline PATH`, `--evolved PATH` | `compile` | The two Qiskit source folders (required) |
| `--store DIR` | `compile` | The baseline store. Required unless `QTB_STORE` is set; the flag wins. There is no default, and the directory must already exist |
| `--profile NAME` | `compile` | `iterations-profile` (default) or `confirm-profile` |
| `--results-root DIR` | every command | The session directory (default `results/`) |

The stages take no other options. The worker count is `LSB_DJOB_NUMPROC` when LSF sets it,
otherwise one less than the CPU count, at most 12.

## One session per results root

`--results-root` *is* the session directory. It holds one baseline/evolved pair: the
snapshots, the evolved build, the verifier, every stage's output and the verdict. There is
no `runs/` level inside it; use a new directory for each pair.

- **`compile` creates the session.** It refuses a directory that is already a session for
  other sources, another store or another profile, and a directory that exists but is not a
  session (exit 64). Run again with the same arguments, it resumes an unfinished `compile`.
- **The later stages take only `--results-root`.** They read the profile and the store from
  the session's `run.json`, so every stage of a session uses the same store.
- **A finished stage never runs again.** A `complete` or `skipped` stage prints
  `already complete` (or `already skipped`) and exits 0, so a whole chain can be resubmitted
  safely. A stage that failed or was killed resumes where it stopped when you run it again.
- **To measure again, start a new session.** With the store, a new session pays only for
  the evolved build and the evolved half of each stage.
- **The harness must not change during a session.** A stage refuses to run (exit 41) when the
  harness code or profile differs from what `compile` recorded; the message names the
  archived harness wheel. `decide` uses the session's archived profile and still works with
  a newer harness.
- **After `compile`, edits to the source folders do not reach the session.** Builds come from
  the snapshots.

## Reusing a baseline

The store holds baseline data only: baseline builds and the baseline results of `quality`,
`correctness` and `unit-tests`. Nothing about the evolved tree, and no cost sample, is ever
written to it. Several sessions, on several hosts, can use one store at once.

The first session against a baseline builds it and fills the store. A later session with the
same baseline logs `Reusing baseline build <key> from the store` and builds only the evolved
tree; its stages then run only the evolved half. The report lists what came from the store.
The baseline is reused only when **all** of these hold:

- **Same store.** Pass the same `--store` (or `QTB_STORE`).
- **Same baseline content.** The tree hash of the snapshot counts, not the folder path. A new
  commit or any edited file in the baseline folder is a new baseline.
- **Same harness version, lock files, Rust toolchain and Python.** The harness wheel hash is
  part of every key, so an edit to the harness code breaks result reuse: the baseline
  quality, correctness and unit-test results are recomputed. It does not repeat the Rust
  compile: the stored wheel is reused, and only a new virtual environment is installed.
  While you are changing the harness, expect no baseline result reuse.
- **Same CPU model** for the baseline quality, correctness and unit-test results. The build
  itself depends only on the OS and architecture.

An A/A session (the same source for both revisions) reuses the baseline build but never the
stored baseline results, so it stays an independent check of the harness. A failed
determinism audit invalidates the matching stored baseline quality results.

The store is maintained by hand. `du -sh $QTB_STORE/*` shows its size: about 330 MB per
baseline build, plus a few MB of results. Delete an entry, or the whole store, when no session
is running against it; every entry can be recomputed. A session whose baseline build was
deleted cannot run more stages (exit 41), but its `decide` still works. A `builds/<key>/`
directory without a `READY` file is an interrupted build; a later `compile` repairs it.
Caches under an old `results/` directory (`build-cache/`, `quality-cache/`,
`verifier-cache/`) are not migrated.

## The quality gate

At the end of `quality`, the gate decides whether the expensive stages run. It is stored in
`stages/quality/state.json` with its reason, and cannot be overridden.

| Gate | When | Then |
| --- | --- | --- |
| `improved` | Every quality check passed (round-trip, audit, C0, C6, C1-lite, guards, caps, completeness) and every improvement record passed | `correctness`, `unit-tests` and `cost` run |
| `aa` | Both builds have the same ID (an A/A session) and every quality check passed | They run as a check of the harness; the cost panels are the known-outcome check |
| `closed` | Anything else: no improvement, an unresolved improvement, or a failed or unresolved quality check | They record `skipped` with the reason and exit 0 within seconds |

`cost` checks once more: if `correctness` found a failure, `cost` records `skipped`.

**What `NO_IMPROVEMENT` checks.** Both revisions built and compiled identical inputs, no
quality check failed, and the candidate showed no gain beyond seed noise. **It does not check
correctness.** With the gate closed, the C1–C5 suite, the API contracts, the C7
Clifford checks, the upstream tests and the cost panels never run, so an incorrect candidate
without an improvement also ends `NO_IMPROVEMENT`. The report says so: "Correctness, unit
tests and cost not checked: the quality gate is closed". It also says whether the
baseline's correctness is known from the store. A failed quality check (C0, C6 or a guard)
still gives `CONSTRAINT_VIOLATION`, without the correctness suite.

If the baseline itself fails its correctness checks, the evolved revision is not checked,
the quality evidence is set aside, and the verdict is `INCONCLUSIVE`. Without an
improvement, correctness never runs, so a broken baseline is found by the first session
against it that gets through the gate.

## Reading the verdict

`decide` prints the verdict and the session directory. It writes `decision.json` (for
machines) and `report.md` (for people) into the session directory. The report starts with a
table of every stage: its state, host, duration and, for a skipped stage, the reason. See
[output format](docs/output-format.md).

| Verdict | Exit | Meaning | What to do next |
| --- | ---: | --- | --- |
| `PASS` | 0 | Depth improved and every required constraint passed | On iterations: run confirm. On confirm: you have a claim for this workload |
| `NO_IMPROVEMENT` | 10 | No gain beyond seed noise, and no quality check failed. Correctness is not checked when the gate is closed (see above) | Keep iterating |
| `CONSTRAINT_VIOLATION` | 20 | The candidate failed a quality or correctness check, or a cost guard showed a real regression | Read "Constraints needing attention" in `report.md` |
| `INCONCLUSIVE` | 30 | Something required is missing or unresolved, a required stage has not finished, or the baseline itself failed | Read the listed reasons; run any unfinished stage and `decide` again |
| `ERROR` | 40 | The harness, a build or an input failed | Check the build or worker logs named in the error |

A required stage that has not finished keeps the verdict `INCONCLUSIVE`, so `decide` can run
while stages are still going. `compile` and `quality` are always required, `correctness`
when the gate is open, `cost` when the gate is open and correctness found no failure, and
`unit-tests` once it has started.

Changes to the optimization stage, two-qubit synthesis or shared infrastructure are
not verified at full scale by any check, so on the iterations profile they reach at most
`INCONCLUSIVE`. Layout and routing changes are fully covered. The change scope is always
inferred from the changed files. See
[stage coverage](docs/metrics.md#7-change-scope-and-stage-coverage).

## Optional unit tests

`unit-tests` runs, from the baseline tree, the upstream Python tests in
`test/python/transpiler` and `test/python/compiler` and the Rust tests
(`cargo test -p qiskit-transpiler`), on both builds. It also runs the evolved tree's own
Python tests, for the report only. It works on both profiles, and no profile requires it.

The rule is: **if you run it, it counts.** Once `unit-tests` has started, `*1/upstream` is
required. A new failure on the evolved build gives `CONSTRAINT_VIOLATION`, a stage that is
still running gives `INCONCLUSIVE`, and a failed stage gives `ERROR`. If it never started, or
the gate skipped it, the verdict is decided without it and the report says "Upstream tests:
not run." If you start `unit-tests` after `decide`, run `decide` again.

## Exit codes of the stages

A stage's exit status says whether it ran, not what it found. A correctness mismatch or a
cost breach is a finding: it goes into the evidence, the stage is complete, and the exit
status is 0. Only `decide` turns findings into a verdict and exits with the verdict's code
(table above).

| Exit | Meaning |
| ---: | --- |
| 0 | The stage is complete or skipped (also when it already was) |
| 40 | The stage failed: a harness, build or worker error. Its state is `failed`, and `decide` gives `ERROR` |
| 41 | A precondition is not met: an earlier stage is missing or unfinished, the directory is not a session, the session is cleaned or being cleaned, the harness or profile changed since `compile`, the store or the baseline build is missing, this host cannot run the builds, or the same stage is already running |
| 64 | Usage error: bad options, no store given, the store does not exist, or (for `compile`) the results root is another session's or not a session |

`decide` on a directory that is not a session exits 41. `clean` exits 0 or 41.

## Cleaning up a session

```bash
uv run qiskit-transpile-bench clean --results-root "$S"
```

`clean` works on one session and never touches the store. It runs only after `decide` has
seen every stage's final state: it refuses (exit 41) while a stage is running (a killed job
must be run again to a final state first), before `decide`, and when a stage changed after
the last `decide`, for example when `unit-tests` was started later. It ignores the verdict.

- **Deletes:** the evolved build, the source snapshots, the verifier and its cache, worker
  scratch directories, `oracle-jobs/`, the copied upstream test trees and their
  session-local `CARGO_HOME` crates, and verified quality and C6 prefix outputs larger than
  the policy's `output_retention_bytes`. Failing or unverified outputs are kept.
- **Keeps:** `run.json`, the archived profile, `harness-wheel/`, `stages/`, `evidence.json`,
  `decision.json`, `report.md`, the logs, `observations.jsonl`, `correctness.jsonl`,
  `clifford.jsonl`, `cost/`, `changed-tests.json`, every `job.json` and the snapshot
  manifests.

`clean` records every planned deletion in `clean.json` before deleting anything, and a killed
`clean` resumes from that list when run again. It prints the space freed. Afterwards `decide`
still works, and every other stage exits 41. To remove a session entirely, delete its
directory.

Some cleanup is automatic: `compile` deletes each build's `target/release` once the wheel is
installed and checked, and `unit-tests` deletes its Rust test build when it ends and keeps
failure text only for failed tests.

## Running on a cluster

Each stage can be its own job on its own host. The session directory and the store must be
on a shared filesystem mounted at the same path on every node, and so must the `uv` Python
directory (usually `~/.local/share/uv/python`). Only `cost` needs a quiet, exclusive node.

`tools/lsf/submit.sh NAME BASELINE EVOLVED COST_MODEL [PROFILE] [--no-unit-tests]` is an
example that submits a whole session to LSF as dependent jobs. Its `cost` job
(`tools/lsf/cost_if_gated.sh`) records `skipped` locally when cost would be skipped, and
otherwise runs `cost` on an exclusive host of model `COST_MODEL`. Adapt the queues, slot
counts and walltimes to your site. See [environments](docs/environments.md).

## How long it takes

Measured on an Apple M1 Max laptop. See [environments](docs/environments.md#how-long-it-takes)
for details.

| Step | Time |
| --- | --- |
| Building one Qiskit revision from source | 20–60 min measured with Qiskit's fat-LTO profile, one revision after another. Builds now use thin LTO and compile both revisions at once, which should be much faster (not yet timed). The baseline build is kept in the store; the evolved tree is built in every session |
| Whole `compile`, first time | About 1 h 20 min, almost all of it building both revisions. With the baseline in the store, only the evolved revision is built |
| Iterations quality compiles | About 10 CPU-minutes per revision, roughly doubled by routing replay |
| Confirm quality compiles | About 3.5 CPU-hours for the candidate once the baseline is in the store |
| Cost panels, iterations | One round (2 arms × 16 cases) is about 1.5 min. Typical: the 4-round screen, about 6–7 min. Worst: screen + 6-round full measurement + 12-round rerun = 22 rounds, about 35 min. Add about 10–20 min for the multi-seed companion when the change touches layout or routing |
| Cost panels, confirm | One round (2 arms × 36 cases) is about 3.5 min. Typical: the 4-round screen (about 14 min) plus memory (about 10 min), about 25 min. Worst: screen + 10-round full + 20-round rerun = 34 rounds, about 2 h, plus a memory rerun |
| Correctness checks (C1–C5, C7) | Compiles run in parallel, and the verifier checks each distinct output once, in batches. Verifier results are cached by content in the session's `verifier-cache/`, so a retried stage re-verifies only new outputs. The baseline half comes from the store after the first session against that baseline |
| Upstream tests (optional `unit-tests`) | Three Python runs of about 3–4 min each, plus Rust tests for both builds (budgets of 4 h Python and 3 h Rust). The baseline runs come from the store after the first session against that baseline |

Cost panels are estimates from the design probes, not measured runs: no full comparison with
cost measurement has been recorded yet. They run only when the quality gate is open and
correctness found no failure, on an otherwise idle machine. There is no calibration step: the
cost thresholds are fixed in `policy.json`. Each stage writes its own
`stages/<stage>/progress.log` with the wall time of each step, and `decide` writes
`progress.log` next to `report.md` with every stage's log and one table of step durations.

**Why the cost panels are this fast.** Each panel starts with a 4-round *screen*. If the
candidate's compile time already sits inside the noise band of the full measurement (and no
single case is more than 10% slower), the panel stops there. Otherwise it is measured in full
(6 rounds in the iterations profile, 10 in confirm) and, on a candidate-only breach, once more
with doubled rounds. Every round is one fresh process per arm that times the whole panel, so
Python and Qiskit start-up is paid twice per round instead of once per case. The `cx`/`ecr`
twins of the scored circuits are timed only when the change can reach translation or
optimization, since they repeat the `cz` cases' layout and routing. See
[iterations-profile.md](docs/iterations-profile.md#timing-panels) and
[metrics.md](docs/metrics.md#6-cost-time-and-memory).

**Cost thresholds are fixed, not calibrated.** A panel fails when the candidate is more than
3% slower overall (`panel_ratio`), or when any single case is more than 10% slower *and*
slower by more than 25 ms (32 MiB for memory), so jitter on millisecond cases never counts.
The screen must clear half of the 3% band. These numbers come from the design probe on an
idle M1 Max; the harness never measures your machine's own noise. To check them on a new
runner, run a session with the same source folder as both `--baseline` and `--evolved` once.
The quality panel is an exact tie, the gate opens as `aa` if every quality check passes, and
the cost rows of `report.md` show how far two independent builds of the same code drift
apart. If that drift is more than about 2% (six
tenths of the band), raise `panel_ratio` in a new policy version rather than trusting cost
failures on that machine.

Disk: about 330 MB per baseline build in the store once `target/release` is removed, plus a
few MB of results per baseline. A session keeps its evolved build, the verifier and every
stage's output until you run `clean`.

## Reproducibility guarantees

- Every quality panel uses seeds 0–99 (block B0).
- Workers run serially (`QISKIT_PARALLEL=FALSE`, one Rust and BLAS thread, `PYTHONHASHSEED=0`),
  and a determinism audit recompiles a sample to prove outputs are reproducible.
- Baseline quality results are stored by build, case definition, CPU model, harness and
  measurement protocol, so a new session against the same baseline only compiles the
  candidate. A failed determinism audit invalidates the stored baseline quality results it
  covers.
- Cost samples belong to one session and are never stored or reused: both arms are measured
  again in every session.
- Profiles and fixtures are frozen and hashed. No comparison regenerates its inputs.
  Six inputs whose redistribution terms were unclear were replaced by recorded Qiskit
  constructions; see [fixture provenance](fixtures/PROVENANCE.md).
- A changed fixture, weight, threshold or canary value requires a new profile version. See
  [versioning](docs/versioning.md).

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](docs/architecture.md) | Components, trust boundaries, process model, stage graph and gate, session directory and store, source layout |
| [Iterations profile](docs/iterations-profile.md) | The default workload, acceptance rules IA1–IA6, statistical power, budget |
| [Confirm profile](docs/confirm-profile.md) | The broad workload, families and weights, rules CA1–CA6, report-only numbers |
| [Metrics](docs/metrics.md) | `D2`/`N2`, scores and paired standard errors, guards, cost estimators and thresholds, stage coverage, verdict |
| [Verifier](docs/verifier.md) | The pinned semantic oracle and every correctness check (C0–C7, C1-lite, upstream tests) |
| [Environments](docs/environments.md) | `envs/` lock files, how builds work, where they live, build time and disk use, troubleshooting |
| [Output format](docs/output-format.md) | Session directory, `decision.json`, `report.md`, observations, circuit files |
| [Implementation status](docs/implementation-status.md) | What is implemented and validated, and how to validate a new runner |
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
