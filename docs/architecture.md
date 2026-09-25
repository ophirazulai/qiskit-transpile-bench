# Architecture and components

This page explains how the harness is put together: which parts exist, what each one is
allowed to do, and how a comparison flows through its stages. For the reasoning behind every
rule, see the design plans in `design/transpilation-benchmark-impl-plan.md` and
`design/split-compare-stages-plan.md`. For what is implemented and validated, see
[implementation-status.md](implementation-status.md).

## The problem it solves

You have two Qiskit source folders: a **baseline** (the reference) and an **evolved** revision
(a candidate change to the transpiler). The question is:

> Does the evolved revision produce circuits with lower native two-qubit depth on a fixed
> workload, without breaking correctness, getting worse on other quality measures, or
> becoming slower or larger to compile?

Answering this fairly is hard because the candidate is itself Qiskit. If the candidate
computed its own score (for example with `QuantumCircuit.depth()`), a change could improve its
score without improving any circuit. The architecture exists mainly to prevent that.

## Trust boundaries

Three rules shape the whole design:

1. **Never load two Qiskit versions in one process, and never pass live Qiskit objects
   between processes.** Components talk only through versioned JSON and JSONL files.
2. **The revision under test never grades itself.** The worker inside a revision's
   environment only compiles and exports. Metrics, legality, routing replay and semantic
   checks are computed elsewhere: by pure-Python harness code or by an independently pinned
   Qiskit (the verifier).
3. **Evaluation is replayable.** The verdict is computed from saved observations and
   evidence. `decide` recomputes it at any time from the session's files and archived
   profile, without compiling anything, also after `clean`.

```text
  baseline folder ─┐                               ┌─ profile: manifest.json, policy.json
  evolved folder  ─┼─► snapshot ─► build wheels ───┤     fixtures (circuits, targets)
                   │   (envbuild)  (2 venvs)       │
                   │                               ▼
                   │                         ┌────────────┐
                   │        job.json ───────►│ coordinator│◄──── never imports Qiskit
                   │                         └─────┬──────┘
                   │             ┌─────────────────┴─────────────────┐
                   ▼             ▼                                   ▼
          worker (baseline venv)  worker (evolved venv)         verifier venv
          imports that revision   imports that revision         Qiskit 2.5.2
                   │                    │                            │
                   └─ outputs, layouts, timings                      oracle results
                                        │                            │
                                        ▼                            │
                         observations.jsonl, evidence.json  ◄────────┘
                                        │
                                        ▼
                         evaluator ─► reporter ─► decision.json + report.md
```

## Components

| Component | Location | Imports Qiskit? | Responsibility |
| --- | --- | --- | --- |
| CLI | `src/qtb/cli.py` | No | `compile`, `quality`, `correctness`, `unit-tests`, `cost`, `decide`, `clean`; exit codes |
| Profile configuration | `profiles/*/manifest.json`, `policy.json`, `src/qtb/config/` | No | Versioned workloads (cases, roles, weights, seeds) and thresholds; JSON Schemas for every file format |
| Fixtures | `fixtures/` | No | Frozen canonical circuits, targets, semantic references, the C1–C5 correctness suite, provenance and licenses |
| Canonical IO | `src/qtb/canonical/` | No | Deterministic JSON, hex floats, gzip circuit files, SHA-256 hashing, atomic writes |
| Environment builder | `src/qtb/envbuild/` | No | Snapshot source folders, build release wheels in isolated venvs, record provenance ([environments.md](environments.md)) |
| Coordinator | `src/qtb/coordinator/` | No | Stages and their locks, the gate, the baseline store, job scheduling, timeouts, determinism audit, cost sessions, upstream tests, `decide` and `clean` |
| Worker | `worker/qtb_worker/` | Yes, the revision under test | Rebuild inputs from canonical data, compile, export outputs, time compiles, measure memory, run live API checks |
| Metrics | `src/qtb/metrics/` | No | Streaming `D2`/`N2`, target legality (C0), layout validation, exact routing replay (C6) |
| Verifier | `verifier/qtb_verifier/` | Yes, pinned Qiskit 2.5.2 | Semantic oracles: C1, C2/C1-lite, C3, C5, C7 ([verifier.md](verifier.md)) |
| Evaluator | `src/qtb/evaluator/` | No | Paired log-ratio estimators, guards, change scope and stage coverage, cost guards, verdict ([metrics.md](metrics.md)) |
| Reporter | `src/qtb/reporter/` | No | Writes `decision.json` and `report.md` ([output-format.md](output-format.md)) |
| Tools | `tools/` | Some | Fixture curation (`tools/curate/`), probes, timeout freezing, schema generation. Not used at comparison time |

The harness ships as one wheel (`qiskit-transpile-bench`) that contains all three Python
packages: `qtb`, `qtb_worker` and `qtb_verifier`. The profiles, fixtures and lock files are
bundled as `qtb/data`. The same wheel is installed into every revision environment and the
verifier environment. Each process uses only the package that belongs to it.

## Process model

The coordinator runs in your `uv` environment and starts every other process itself:

- **Worker:** `<revision-env>/bin/python -P -m qtb_worker --job job.json --out out/`.
  It runs from a scratch directory, with a sanitized environment and a serial configuration
  (`QISKIT_PARALLEL=FALSE`, `RAYON_NUM_THREADS=1`, `PYTHONHASHSEED=0` and so on; see
  [environments.md](environments.md#runtime-environment-of-a-worker)). Before doing anything,
  it checks that `qiskit` and `qiskit._accelerate` load from inside its own venv and that
  the native extension's SHA-256 matches the build record.
- **Verifier:** `<verifier-env>/bin/python -P -m qtb_verifier --job job.json --out result.json`.
  It refuses to run unless the installed Qiskit is exactly 2.5.2.

A worker job covers one case and a batch of seeds (up to 25 for quality). The worker appends one
JSON line to `out/results.jsonl` per finished seed and fsyncs it. The coordinator treats each
line as a heartbeat. If no new line appears within the case's `timeout_s`, it kills the
process group, records the stuck seed as an error, and reruns the remaining seeds in a fresh
process.

### Worker modes

| Mode | What the worker does | Output |
| --- | --- | --- |
| `roundtrip` | Rebuild the input circuit and target from canonical data, then export them again | Hashes that must equal the frozen fixture hashes |
| `quality` | Build the preset pass manager for (target, options, seed), compile, export | Canonical output circuit, layout, pipeline fingerprint, compile time |
| `prefix` | Like `quality`, with `pipeline_edits` such as `drop_stage:optimization` | Truncated-pipeline outputs for routing replay and Clifford checks |
| `timing_e2e` | Warm up, then time complete `transpile()` calls | Raw nanosecond samples |
| `timing_reuse` | Build the pass manager outside the clock, then time `pm.run()` | Raw samples |
| `timing_batch` | Run every `timing_e2e`, `timing_reuse` and `preset_build` case of a panel in one process, one result row per case | Raw samples per case |
| `preset_build` | Time `generate_preset_pass_manager(...)` alone | Raw samples |
| `memory` | One compile in a fresh process | Setup and peak RSS in bytes |
| `diagnostics` | One untimed compile with a pass callback | Per-pass times (report-only) |
| `api_checks` | Pass-manager contracts that need live objects | Input immutability, reuse without leaks, batch order, expected errors |

## Stages and the gate

There is no single command that runs a comparison. Each command runs one stage against one
**session directory** (`--results-root`), and each stage can run in its own process, on its
own host. The stages are implemented in `src/qtb/coordinator/stages.py` (`run_compile`,
`run_stage`), `decide.py` and `clean.py`; the work itself is done by `Comparison` in
`src/qtb/coordinator/__init__.py`.

```text
                  compile                 (busy node)
                     |
                  quality                 (busy node; computes the gate)
                     |
             gate open?  -- no -->  correctness, unit-tests and cost record "skipped"
                     | yes
            +--------+---------+
            |                  |
       correctness       [unit-tests]    (parallel; busy nodes; unit-tests optional)
            |                  |
          cost                 |          (exclusive quiet node; skipped if correctness failed)
            |                  |
            +--------+---------+
                     |
                  decide                  (any node; seconds)
                     |
                 [clean]
```

1. **`compile`** creates the session directory. It records the sources, the store and the
   profile in `run.json` and archives the manifest and policy with their hashes. It then
   snapshots both source folders, computes the changed files and the
   [change scope](metrics.md#7-change-scope-and-stage-coverage), builds the harness wheel
   and computes both build identities. The baseline build comes from the store, or is built
   into it on a miss; the evolved build is made in the session at the same time. Then it
   builds the verifier environment and runs the **round-trip**: every revision rebuilds every
   input circuit and target and must reproduce the frozen hashes (`harness/roundtrip`). This
   proves that both revisions compile identical inputs.
2. **`quality`** compiles every non-timing case of the profile, for both revisions, on seed
   block B0. Each output gets C0 (structure) and, where applicable, C6 (routing replay) and
   C1-lite (confirm profile). Baseline observations come from the store when possible. Up to
   9 seed batches compile at once (quality metrics are deterministic and these compile times
   are never read); results are committed in plan order. The **determinism audit** then
   recompiles at least 5% (minimum 10) of observations, half of them with a different
   `PYTHONHASHSEED`, and they must reproduce identical outputs. The recompiles run
   concurrently. Last come the aggregate checks (C0, C6, completeness, C1-lite) and the
   **gate**.
3. **`correctness`** runs the baseline half first: the frozen C1–C5 suite, the API contracts
   and the C7 Clifford variants on the baseline, or their stored results. It records
   `baseline/preflight`. If the baseline itself failed, the stage stops there and the
   evolved revision is not checked. Otherwise it runs C1–C5 and the API contracts on the
   evolved revision, then C7.
4. **`unit-tests`** (optional) runs the baseline's upstream Python tests
   (`test/python/transpiler`, `test/python/compiler`) and Rust tests
   (`cargo test -p qiskit-transpiler`) against the baseline build, or takes their stored
   results, then against the evolved build. Upstream failures the baseline shares are
   known-bad and never count against the candidate. The evolved tree's own Python suite is
   run for the report only.
5. **`cost`** measures the timing and memory panels. It checks the quality and correctness
   evidence again and records `skipped` if correctness found a failure. Measurement waits up
   to 5 minutes for the load average to settle. The two arms (baseline, evolved) are
   interleaved in random order, each round in a fresh process per arm. A short screen ends a
   clearly clean panel early; otherwise the panel is measured in full and, on a
   candidate-only breach, rerun once.
6. **`decide`** merges the committed evidence of every stage, adds `*1/stage-coverage` when
   both `quality` and `correctness` are complete, applies the stage rules below, and has the
   evaluator and reporter write `decision.json`, `report.md` and the merged `evidence.json`
   and `progress.log`.
7. **`clean`** deletes the session's bulk after `decide` and keeps its results.

### The gate

At the end of `quality`, `gate()` evaluates the quality records together with the compile
and quality evidence, and writes `gate` and `gate_reason` into `stages/quality/state.json`.
It cannot be overridden.

| `gate` | When | Then |
| --- | --- | --- |
| `improved` | Every non-improvement record passed (round-trip, audit, C0, C6, C1-lite, guards, caps, exact guards, completeness, `failure/*`) and every improvement record passed (`IA2/improvement`; on confirm also `CA3/breadth`) | `correctness`, `unit-tests` and `cost` run |
| `aa` | Both builds have the same ID (an A/A session) and every non-improvement record passed | They run as a check of the harness; the A/A cost panels are the known-outcome check |
| `closed` | Anything else: no improvement, an unresolved improvement, or a failed or unresolved quality check, also on an A/A session | They record `skipped` with the reason and exit 0 within seconds |

Running quality before correctness changes which evidence exists, not the verdict function:

- **No improvement:** `NO_IMPROVEMENT`, even for an incorrect candidate, because correctness
  never ran. The report says that correctness, unit tests and cost were not checked, and
  whether the baseline's correctness is known from the store.
- **A failed quality check** (C0, C6, a guard): `CONSTRAINT_VIOLATION`, without the
  correctness suite.
- **Improvement, then a correctness failure:** `CONSTRAINT_VIOLATION`; `cost` is skipped.
- **Improvement, but the baseline fails its correctness preflight:** `decide` sets aside the
  quality evidence (it keeps the compile and correctness evidence and any harness records),
  so the verdict is `INCONCLUSIVE`. The report notes it.

### Rules every stage follows

1. **Locks.** Take the session's `lifecycle.lock` shared, then `stages/<stage>/lock`
   exclusive. Both are non-blocking: if either is held, exit 41.
2. **Refuse a cleaned session**, or one whose `clean` was interrupted (exit 41).
3. **Never rerun a finished stage.** A `complete` or `skipped` stage prints
   `already complete` (or `already skipped`) and exits 0.
4. **Check the harness.** Load `run.json` and compare the coordinator, implementation,
   manifest and policy hashes with the current harness. A change since `compile` exits 41,
   and the message names the archived harness wheel.
5. **Check prerequisites.** `quality` needs `compile`; `correctness` and `unit-tests` need
   `quality`; `cost` needs `correctness`. Each must be `complete` or `skipped`, otherwise exit
   41 with the command to run.
6. **Check the store.** The store in `run.json:store` and the baseline build entry must exist.
7. **Check the gate** (gated stages only). If the stage should not run, write `skipped` with
   the reason and exit 0.
8. **Check the host.** The OS, architecture and Python version must match the build identity,
   and the build's Python must exist and run. Otherwise exit 41.
9. **Write `running`, do the work, commit the evidence, then write `complete`.** The final
   `state.json` is the commit marker. A crash writes `harness/error/<stage>` and `failed`
   (exit 40). A successful retry drops the stage's old crash record.

A stage's exit status reports whether it ran, not what it found; findings go into evidence.

A stage that is `running` (after a killed job) or `failed` resumes when run again:

- `compile` keeps a finished evolved build;
- `quality` skips seeds already in `observations.jsonl`;
- `correctness` truncates `correctness.jsonl` and `clifford.jsonl`, resets its evidence and
  restarts; the verifier cache makes repeated checks cheap;
- `unit-tests` resets its evidence and replaces its results;
- `cost` reuses valid regime bundles.

To measure a finished stage again, start a new session.

### What `decide` does

`decide` holds `lifecycle.lock` shared and `stages/decide.lock` exclusive while it takes one
snapshot of every stage's state, the state files' hashes and their committed evidence. A
`complete` stage contributes all its evidence, a `failed` stage only `harness/error/<stage>`,
and a `running`, `skipped` or unstarted stage nothing. A record ID that appears in two stage
evidence files is a harness error.

It uses the session's archived `manifest.json` and `policy.json`, checked against the hashes
in `run.json`, so it works with a newer harness and after `clean`. It never rewrites
`run.json`.

Required stages are conditional: `compile` and `quality` always, `correctness` when the gate
is `improved` or `aa`, `cost` when the gate is open and correctness found no failure, and
`unit-tests` once it has started (`running`, `failed` or `complete`), in which case
`*1/upstream` joins the required IDs. A required stage that has not finished forces
`INCONCLUSIVE` (unless the verdict is `ERROR`). Cost evidence is replayed from the raw bundles
only when `cost` is complete.

`decision.json` records each stage's status, whether it was required, its host, duration,
attempts and what it reused, plus the stage-state hashes that `clean` checks. `report.md`
starts with a table of the stages and the notes. The top-level `progress.log` is the stage
logs in graph order followed by one table of step durations for all stages.

## Who writes what

Stages may run at the same time on different hosts, so **every shared file has exactly one
writer**, and each stage's outputs are its own.

```text
RESULTS_ROOT/                             the session
  run.json                                compile only (sources, store, profile, hashes,
                                          builds, scope, changed paths, compile host)
  manifest.json, policy.json              compile (the archived profile; decide reads these)
  harness-wheel/, harness-build.log       compile
  builds/<rev>/, builds/<rev>.snapshot.json   compile (source snapshots of both trees)
  builds/evolved-build/                   compile (the baseline build is in the store)
  verifier/                               compile
  verifier-cache/                         any stage (content-addressed, atomic writes)
  observations.jsonl                      quality
  correctness.jsonl, clifford.jsonl       correctness
  upstream-baseline/, upstream-evolved/,  unit-tests
  upstream-evolved-own/, upstream-evolved-own.json, changed-tests.json
  cost/                                   cost
  jobs/<uuid>/, oracle-jobs/<uuid>/       any stage (unique names)
  stages/<stage>/state.json               that stage
  stages/<stage>/evidence.json            that stage
  stages/<stage>/progress.log             that stage
  stages/<stage>/lock                     that stage's exclusive lock
  stages/decide.lock                      decide
  evidence.json, decision.json,           decide
  report.md, progress.log
  lifecycle.lock                          shared by stages and decide, exclusive for clean
  clean.json                              clean
```

`stages/<stage>/state.json` (format `qtb-stage/1`) records the status (`running`,
`complete`, `skipped` or `failed`), times, attempts, the host (`machine`), LSF variables when
set, the input hashes and build IDs, the worker count, step durations, what came from the
store (`reused`), and, for `quality`, the gate. See [output-format.md](output-format.md).

## Session directory and store

| | Session directory | Store |
| --- | --- | --- |
| Given by | `--results-root`, on every command | `--store` (or `QTB_STORE`), on `compile` only; required, no default, must already exist |
| Holds | One baseline/evolved pair: the snapshots, the evolved build, the verifier, every stage's output, the verdict | Baseline data only: baseline builds and the baseline results of quality, correctness and unit tests |
| Lifetime | One experiment; `clean` shrinks it | Many sessions; maintained by hand |
| Shared by | Nobody else | Every session that names it |

`compile` records the store once, as an absolute path, in `run.json:store`. Later stages read
it from there. Nothing about the evolved tree is written to the store: not the evolved build,
the verifier, the verifier cache, evolved observations or any cost sample.

### Store layout

| Path | What | Key covers | Written when |
| --- | --- | --- | --- |
| `STORE/builds/<key>/` | A ready baseline build: `READY`, `build.json`, `env/`, `source/`, `cargo/`, `wheels/`, `build.log` | Build identity (snapshot tree hash, Python, lock files, Rust toolchain, native compilers, build flags, OS, architecture) and the harness wheel hash | `compile` built it, removed `target/release` and `verify_build` passed; `READY` is written last |
| `STORE/builds/<key>.lock` | Held while that key is being built | | |
| `STORE/wheels/<identity>/` | Baseline Qiskit wheels | Build identity | The Rust compile finished. A harness change gives a new `builds/` key but the same wheel, so it costs a venv, not a Rust compile |
| `STORE/quality/<key>/` | Baseline quality observations and their output and job files; `invalidated.json` after a failed audit | Build ID, case definition, CPU model of the `quality` host, worker protocol, implementation, measurement protocol, block and mode, worker environment | Per seed |
| `STORE/correctness/<key>/` | `rows.jsonl`, `evidence.json`, `provenance.json`: the baseline rows of `correctness.jsonl` and `clifford.jsonl` and the baseline behavior, API and C7 records, `*1/C1-C5/baseline` and `baseline/preflight` | Build ID, implementation, harness, correctness suite, manifest and policy hashes, verifier identity, CPU model, worker environment, Clifford seed count and mode, tolerances | Every baseline check was decisive (`verified` or `mismatch`) and no worker failed or timed out |
| `STORE/unit-tests/<key>/` | `python.json`, `rust.json`, their logs, `provenance.json` | Build ID, harness, `dev-tests.lock`, the baseline test tree, test budgets, CPU model, worker environment | Both baseline suites completed |
| `RESULTS_ROOT/verifier-cache/<xx>/<key>.json` | Decisive (`verified`/`mismatch`) verifier results, per session | Output, reference and target file hashes, oracle options, harness implementation and verifier locks | Any stage; entries are re-checked before reuse |

How the store is used:

- **Finding a build.** `compile` computes the baseline build identity before any build work
  and looks for `STORE/builds/<key>/READY`. On a hit it checks that `build.json` and its
  snapshot point inside the entry and that `verify_build` passes, then logs
  `Reusing baseline build <key> from the store`. On a miss it takes `<key>.lock`, checks
  again, removes an incomplete directory, and builds directly at the final path, because a
  virtual environment cannot be moved. Readers ignore a directory without `READY`.
- **A ready build is never modified.** `unit-tests` gives `cargo test` a session-local
  `CARGO_HOME` and `CARGO_TARGET_DIR`, never pip-installs into the stored environment (the
  test dependencies are installed at build time), and runs pytest with
  `PYTHONDONTWRITEBYTECODE=1`. Workers run with it too.
- **Results.** On a hit, a stage copies the stored rows and records into the session, marks
  each with `cached_from: <key>`, and runs only the evolved half. The aggregate records are
  computed as before. Correctness and unit-test entries are written in a private directory
  and renamed into place.
- **A/A sessions** (the same build ID for both revisions) never use stored baseline results,
  but they reuse the baseline build.
- **A failed determinism audit** marks the matching baseline quality entries invalidated.
  Their files stay; readers skip them. It does not invalidate correctness or unit-test
  entries.
- **Cost samples are never stored.** Both arms are measured in the same session.
- **Maintenance is by hand** (see the README). Caches from before the store
  (`results/build-cache/`, `results/quality-cache/`, `results/verifier-cache/`) are not
  migrated.

## Running across hosts

The session directory and the store must be on a shared filesystem mounted at the same path
on every node: virtual environments and job files hold absolute paths, and `run.json:store`
is absolute. The `uv` Python directory (usually `~/.local/share/uv/python`) must be shared
too, or exist at the same path on every node, including the node that first built a baseline
into the store.

Each stage records its own host (`machine`) in its `state.json`. `run.json:machine` is the
`compile` host, for information only. The baseline quality key uses the CPU model of the
`quality` host, and cost bundles record the `cost` host.

The worker count is `LSB_DJOB_NUMPROC` when LSF sets it, otherwise one less than the CPU count,
at most 12. There is no option to change it. `cost` is serial.

| Lock | Held by | Purpose |
| --- | --- | --- |
| `RESULTS_ROOT/lifecycle.lock` | Shared by every stage and `decide`; exclusive by `clean` | `clean` never deletes while a stage or `decide` runs, and no stage starts during `clean` |
| `RESULTS_ROOT/stages/<stage>/lock` | That stage, exclusive | Two invocations of one stage never run at once; the second exits 41 |
| `RESULTS_ROOT/stages/decide.lock` | `decide` | Serializes `decide`'s own rewrites |
| `STORE/builds/<key>.lock` | `compile`, while it builds that key | Two sessions racing on one baseline build it once |
| Runner lock, `/tmp/qtb-runner-<uid>.lock` | Shared by builds, worker jobs and verifier runs; exclusive by `cost` | Timing is never measured while compiles run on the same machine |

The runner lock uses a fixed `/tmp` path, not `TMPDIR`, which LSF sets per job;
`QTB_RUNNER_LOCK` overrides it. On LSF, `cost` gets an exclusive host from `bsub -x`, and the
lock and the load-average wait remain a second line of defence. `tools/lsf/submit.sh` is an
example of a whole session submitted as dependent LSF jobs; see
[environments.md](environments.md).

## Source layout

```text
src/qtb/
  cli.py              command line: the seven commands and their exit codes
  canonical/          hashing and file formats
  config/             profile loading, identities, JSON Schemas (schemas/*.json)
  envbuild/           snapshot, build, provenance
  coordinator/
    __init__.py       Comparison: session, builds, quality, audit, routing replay,
                      aggregation, the gate
    stages.py         running a stage: locks, preconditions, state.json, stage bodies
    decide.py         decide: evidence merge, stage rules, verdict and report
    clean.py          clean: planned deletions and clean.json
    store.py          the baseline store: build entries, result entries
    checks.py         C1–C5 behavior suite, API checks, C7 Clifford checks
    costs.py          panel selection by scope, interleaved two-arm cost sessions, thresholds
    upstream.py       baseline-owned Python tests and Rust tests
    process.py        worker subprocess with heartbeats and timeouts
    storage.py        locks, JSONL records, cache keys, output pruning
    runlog.py         progress logs and step durations
  metrics/            D2/N2, C0 legality, layout validation, C6 replay
  evaluator/          statistics, quality guards, cost guards, scope, verdict
  reporter/           decision.json and report.md
worker/qtb_worker/    adapter.py (Qiskit version adapter), modes.py
verifier/qtb_verifier/ small_exact.py (C1, C7), layout_semantics.py (C2, C1-lite),
                      dynamic.py (C3), schedule.py (C5)
profiles/             iterations-profile/, confirm-profile/
fixtures/             circuits/, references/, targets/, correctness-suite.json, PROVENANCE.md
envs/                 lock files for revision, test and verifier environments
tools/                curation, probes, timeout freezing, schema generation, lsf/ examples
tests/                harness unit tests
design/               design plan and probes
```
