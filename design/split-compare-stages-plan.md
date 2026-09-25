# Plan: replace `compare` with separately runnable stages and a baseline store

Status: implemented on 2026-09-25 (see README.md and docs/architecture.md for the current
behaviour). Written against commit `b3aeae4`. Revised the same day after four reviews
(decisions in §2). This version replaces `compare` and runs the correctness, unit-test and
cost stages only when quality shows an improvement. Stage commands take no options other than
`--results-root`.

## 1. Goal

Today one `compare` process runs everything, in order: snapshot, build, round-trip,
correctness, quality, audit, cost, verdict. This plan replaces it with commands that each do
one stage against one **session directory**:

| # | Command | What it does | Runs when | Node |
| --- | --- | --- | --- | --- |
| 1 | `compile` | Creates the session, snapshots both sources, gets the baseline build from the store (or builds it), builds the evolved revision and the verifier, runs the input round-trip | always | busy |
| 2 | `quality` | Quality compiles, C0, C6 routing replay, C1-lite, the determinism audit, and the **gate** (§4.4) | after `compile` | busy |
| 3 | `correctness` | The C1–C5 suite, API contracts and C7 Clifford checks | gate open | busy |
| 4 | `unit-tests` | *Optional.* The upstream Python and Rust tests | gate open | busy |
| 5 | `cost` | Timing and memory panels | gate open and `correctness` found no failure | **quiet, exclusive** |
| 6 | `decide` | Merges the evidence and writes the verdict and report | any time | any (seconds) |
| 7 | `clean` | Deletes the session's bulk and keeps its results (§11) | after `decide` | any |

**The flow is `compile` → `quality` → stop or continue.** Most evolved trees do not improve
quality. For them, a session costs one evolved build and one set of evolved quality compiles,
then ends `NO_IMPROVEMENT`. The expensive stages (correctness, the upstream tests and the
quiet-node cost panels) run only for a candidate that improved.

Three reasons:

1. **Debugging.** Compile once, then run the later stages one at a time and inspect each
   one's output before going on. A stage that failed or was killed resumes where it stopped,
   without rebuilding Qiskit. A finished stage is not rerun within a session (§5.3).
2. **LSF.** Each stage can be its own cluster job. Only `cost` needs an exclusive, quiet node.
3. **One baseline, many evolved trees.** Everything about the baseline, which is its build
   and its quality, correctness and unit-test results, is kept in a **store** (§10) and
   reused by every later session. A session pays only for its evolved tree.

The same change **removes** `compare`, `evaluate`, `smoke`, qualification, `--change-scope`
and `decision-counts.json` (§9).

### Non-goals

- **No change to what is measured or to the thresholds.** The verdict function
  (`evaluator.verdict`) is unchanged. What changes is which evidence exists: a candidate
  without improvement is not checked for correctness (§6).
- **No LSF code inside the harness.** The harness stays scheduler-agnostic. An example
  submission script lives under `tools/lsf/` (§8).

## 2. Decisions from review

| Question | Decision |
| --- | --- |
| Separate `init`, `compile_baseline`, `compile_evolve`? | **No.** One `compile` command creates the session and prepares both revisions |
| One results root for many runs, or one per session? | **One per session.** `--results-root` *is* the session directory. It holds one baseline/evolved pair. There is no `runs/<id>/` level, so no `--run-id` and no `--run` |
| Tie qualification to the cost host? | **Remove qualification support entirely** (§9.1) |
| Order of the stages? | **`compile` → `quality` → gate.** Correctness, unit tests and cost run only when quality shows an improvement with no failure (§4.4) |
| Should `cost` wait for `unit-tests`? | **No.** `unit-tests` is independent. `cost` waits only for `correctness` |
| Is `unit-tests` required? | **No, it is optional.** If it was run, its result counts (§6.1) |
| How does a new session avoid redoing the baseline? | **A store** (§10), given by a **required** `--store` on `compile`. It holds only baseline data: the baseline build and the baseline results of quality, correctness and unit tests. Everything about the evolved tree stays in the session directory |
| Keep `compare`, `evaluate`, `smoke`, `status`, `gc`? | **No.** `compare` and `evaluate` are removed (§9). `smoke`, `status` and `gc` are not provided. `decide` reports stage states. The store is maintained by hand (§10.6) |
| How is disk space reclaimed? | **`clean --results-root DIR`** (§11) deletes a finished session's bulk and keeps its results. Stages still delete their own build intermediates as they go |
| Keep `--change-scope`? | **No** (§9.3). The change scope is always the one inferred from the snapshot diff |
| Stage options `--workers`, `--rerun`, `--force`? | **No.** A stage takes only `--results-root`. The worker count comes from LSF or the default (§7.1). A finished stage is never rerun; start a new session (§5.3). A closed gate cannot be overridden (§4.4) |

## 3. What `compare` does today and where it goes

From `Comparison.execute` in `src/qtb/coordinator/__init__.py`:

| Step today, in order | Code | New stage |
| --- | --- | --- |
| Snapshot both sources, compute scope | `_build` | `compile` |
| Harness wheel | `_build` | `compile` |
| Qiskit builds (both, concurrently) | `_build` → `build_revision` | `compile`: baseline from the store (§10), evolved in the session |
| Verifier env | `build_verifier` | `compile` |
| Input round-trip | `roundtrip` | `compile` |
| C1–C5 baseline, then `baseline/preflight` | `behavior_checks` | `correctness` (baseline half from the store) |
| C1–C5 evolved | `behavior_checks` | `correctness` |
| C7 Clifford | `clifford_checks` | `correctness` |
| Upstream tests (confirm only) | `upstream_checks` | `unit-tests` (baseline half from the store; optional on both profiles) |
| *Short-circuit:* any failure → verdict | | replaced by the gate after `quality` (§4.4) |
| Quality + C0 + C6 (+ C1-lite on confirm) | `quality` | `quality` (baseline half from the store) |
| Determinism audit | `audit` | `quality` |
| Aggregate checks | `aggregate_checks` | `quality`, except `*1/stage-coverage` → `decide` |
| Cost, only if due | `cost_stage_due`, `measure_costs` | `cost` |
| Qualification | `execute` | **removed** |
| Verdict, decision count, retention pruning | `finish` | `decide` (verdict); `clean` (pruning); decision count **removed** |

The main change in order: **quality now runs before correctness.** Today correctness runs
first, and any failure stops `compare` before quality. §6 lists what this changes in the
verdicts.

Four facts make the split possible:

- Quality, correctness and unit tests use the builds and the verifier, but not each other's
  results. The one exception is `*1/stage-coverage`, which combines quality checks with C7.
  It moves to `decide`.
- The gate and the cost stage need earlier evidence only to decide *whether* to run
  (`cost_stage_due`). Their measurements are independent.
- Every stage's raw output already lives in its own file (`observations.jsonl`,
  `correctness.jsonl`, `clifford.jsonl`, `cost/`, `upstream-*`). Only `evidence.json`,
  `run.json` and `progress.log` are shared, and §5 fixes those.
- `evaluator.verdict` returns `NO_IMPROVEMENT` before it checks that every required record is
  present. A session can therefore end `NO_IMPROVEMENT` with no correctness evidence.

## 4. The commands

```text
qiskit-transpile-bench compile      --baseline PATH --evolved PATH --store DIR
                                    [--profile P] [--results-root DIR]
qiskit-transpile-bench quality      [--results-root DIR]
qiskit-transpile-bench correctness  [--results-root DIR]
qiskit-transpile-bench unit-tests   [--results-root DIR]                           (optional)
qiskit-transpile-bench cost         [--results-root DIR]
qiskit-transpile-bench decide       [--results-root DIR]
qiskit-transpile-bench clean        [--results-root DIR]
```

`--results-root` defaults to `results`, as today.

### 4.1 Session directory and store

| | Session directory | Store |
| --- | --- | --- |
| Given by | `--results-root`, on every command | `--store` (or `QTB_STORE`), on `compile` only. **Required, no default** |
| Holds | One session: one baseline/evolved pair, the evolved build, the verifier, every stage's output, the verdict | Baseline data only: baseline builds and the baseline results of quality, correctness and unit tests (§10.2) |
| Lifetime | One experiment; `clean` shrinks it (§11) | Many sessions; maintained by hand (§10.6) |
| Shared by | Nobody else | Every session that names it |

- **`compile` creates the session.** It refuses a results root that already exists with a
  different session (other sources, store or profile, exit 64), and one that exists and is not
  a session (exit 64). Run on its own unfinished session with the same arguments, it resumes
  (§5.3).
- **The store is recorded once, in `run.json:store`,** as an absolute, resolved path. The
  later stages read it from there and do not accept `--store`, so every stage of a session
  uses the store its baseline came from. If neither `--store` nor `QTB_STORE` is set,
  `compile` exits 64. The flag wins over the environment variable. There is no default
  because a default would let a session that forgot the flag silently use a new, empty store
  and rebuild the baseline.
- **The store must already exist.** `compile` creates the subdirectories but not the store
  root, so a typo in the path exits 64 instead of creating a stray store.
- **Nothing about the evolved tree goes into the store:** not the evolved build, the verifier,
  the verifier-result cache, evolved observations or any cost sample.

### 4.2 Dependency graph

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

### 4.3 What each command does

| Command | Existing code it runs | Prerequisites | Stage-owned outputs |
| --- | --- | --- | --- |
| `compile` | `Comparison.create`, then snapshots, scope, harness wheel, baseline build from the store or into it (§10.3), evolved build, verifier env, then `roundtrip(roundtrip_cases())` | none | `run.json`, `manifest.json`, `policy.json`, `builds/`, `harness-wheel/`, `verifier/`, evidence `harness/roundtrip` |
| `quality` | `quality`, `audit`, `aggregate_checks` **without** stage coverage, then the gate | `compile` | `observations.jsonl`, evidence `failure/*`, `audit/determinism`, `*1/C0`, `*1/C6`, `*6/completeness`, `CA1/C1-lite`, the quality improvement records, `gate` in its `state.json` |
| `correctness` | `behavior_checks` baseline → `baseline/preflight` → (stop if the baseline failed) → `behavior_checks` evolved → `clifford_checks` | `quality`, gate open | `correctness.jsonl`, `clifford.jsonl`, evidence `behavior/*`, `api/*`, `C7/*`, `*1/C1-C5*`, `*1/C7`, `baseline/preflight` |
| `unit-tests` | `upstream_checks` | `quality`, gate open | `upstream-*/`, `changed-tests.json`, evidence `*1/upstream*` |
| `cost` | `cost_stage_due` over quality and correctness evidence, then `measure_costs` | `correctness`, gate open | `cost/`, evidence `*5/*` |
| `decide` | merges completed stage evidence, adds `*1/stage-coverage` when its inputs exist, applies §6, writes the verdict and report | none (a missing stage counts as missing evidence) | `evidence.json`, `decision.json`, `report.md`, `progress.log` |
| `clean` | §11 | `decide` has run after every stage ended | `clean.json` |

Notes on individual commands:

- **`compile` builds the evolved tree in the session,** at `builds/evolved-build/`. There is
  no evolved wheel cache any more (§9.6), so every new session compiles the evolved Rust
  code. Within a session, a resumed `compile` keeps a finished build.
- **`compile` cannot be rerun on a finished session.** Start a new session instead. The
  builds of a session never change once later stages have used them.
- **After `compile`, edits to the source folders do not reach this session.** That is already
  true today, because builds come from snapshots.
- **`decide` can run any time after `compile` creates the session, and as often as you like.** It replaces `status`: the report
  starts with a table of every stage, with its state, host, duration and, for a
  skipped stage, the reason. It reads only evidence from committed `complete` or `skipped`
  stages; a `running` stage contributes no partial records. Required stage states are
  conditional: `compile` and `quality` always, `correctness` when the gate is open, and
  `cost` when the gate is open and correctness found no failure. `unit-tests` counts only
  after it starts (§6.1). A required stage that has not finished gives `INCONCLUSIVE` until
  it is done, even if an earlier improvement record failed. A `failed` stage contributes
  its `harness/error/<stage>` record and gives `ERROR`.
  The exit status is the verdict's exit code, as `compare` returns today. `decide` must add
  the stage-state check before calling the unchanged `evaluator.verdict`, whose
  `NO_IMPROVEMENT` branch otherwise wins over missing required records.

### 4.4 The gate

At the end of `quality`, the stage evaluates its own evidence with `evaluate_quality` and
a new `gate` function and writes the result into `stages/quality/state.json`. `gate` requires
`harness/roundtrip` and every non-improvement record emitted by `quality` to pass, including
the audit, C0, C6, C1-lite, guards and completeness. It must not use `cost_stage_due` unchanged: that function permits
an A/A run with unresolved checks because it only rejects failed checks.

| `gate` | When | Then |
| --- | --- | --- |
| `improved` | All required improvement records and all non-improvement quality records passed | `correctness`, `unit-tests` and `cost` run |
| `aa` | Both builds have the same ID (an A/A run) and all non-improvement quality records passed | They run, as today: an A/A session validates the harness, and its cost panels are the known-outcome check |
| `closed` | Otherwise: no improvement, an unresolved improvement, or a failed or unresolved quality check (C0, C6, a guard, the audit) | They record `skipped` with the reason and exit 0 within seconds. `decide` gives `NO_IMPROVEMENT`, `CONSTRAINT_VIOLATION` or `INCONCLUSIVE` from the quality evidence alone |

`cost` then checks again, as today, over the quality and correctness evidence together. If
correctness found a failure, `cost` records `skipped`.

## 5. Shared state: who writes what

When stages run as parallel processes on different hosts, unrestricted rewrites of
`run.json`, `evidence.json` and `progress.log` would overwrite each other. The rule after the
split:

**Every shared metadata file has exactly one writer; stage outputs are isolated.**

```text
RESULTS_ROOT/                           the session
  run.json                              compile (session inputs, builds, store)
  manifest.json, policy.json            compile
  harness-wheel/                        compile
  builds/<rev>/, <rev>.snapshot.json    compile (snapshots of both sources)
  builds/evolved-build/                 compile (the baseline build is in the store)
  verifier/                             compile
  verifier-cache/                       any stage (content-addressed, atomic writes)
  observations.jsonl                    quality
  correctness.jsonl, clifford.jsonl     correctness
  upstream-*/, changed-tests.json       unit-tests
  cost/                                 cost
  jobs/<uuid>/, oracle-jobs/<uuid>/     any stage (unique names; no conflicts)
  stages/<stage>/state.json             that stage (§5.1)
  stages/<stage>/evidence.json          that stage
  stages/<stage>/progress.log           that stage
  evidence.json, decision.json, report.md, progress.log   decide
  lifecycle.lock                        shared by stages/decide, exclusive for clean
  clean.json                            clean
```

Changes this needs:

1. **Evidence is scoped to a stage.** `Comparison.evidence()` writes to
   `stages/<stage>/evidence.json`. `self.records` holds that stage's records plus a read-only
   view of the other stages' evidence where the stage needs it (the gated stages read the
   quality gate, and `cost` reads correctness). `decide` merges all stage files into
   `evidence.json`. A record ID that appears in two stage files is a harness error.
2. **`harness/error` becomes `harness/error/<stage>`.** A crash is attributed to its stage,
   and two crashes cannot collide. These records are kind `harness` with result `failed`, so
   the verdict is `ERROR`, as today.
3. **Progress logs are per stage.** `decide` writes the top-level `progress.log` by
   concatenating the stage logs in graph order and adding the combined step-duration table.
4. **Caches.** `verifier-cache/` moves into the session. Its keys may be written by several
   stages, so writes must be atomic and an existing key must be verified before reuse. The
   baseline caches move into the store (§10.2). Everything in the store is content-addressed and written with atomic
   `os.replace` (`atomic_bytes`) or protected by a per-key lock and readiness marker, so
   several sessions can use one store at once. A store build key is locked only by `compile`.
5. **`run.json` is compile-owned.** `decide` writes the current verdict and stage summary to
   `decision.json` and `report.md`; it never rewrites `run.json`. This also lets `decide` run
   while `compile` is still working without losing build metadata.

### 5.1 `stages/<stage>/state.json`

```json
{
  "format": "qtb-stage/1",
  "stage": "quality",
  "status": "running | complete | skipped | failed",
  "started_at": "...", "finished_at": "...",
  "machine": { "...": "machine_identity() of the host that ran it" },
  "scheduler": { "LSB_JOBID": "123", "LSB_QUEUE": "normal", "LSB_HOSTS": "..." },
  "inputs": {
    "coordinator": "<coordinator_identity>",
    "implementation": "<implementation_identity>",
    "harness": "<harness wheel hash>",
    "manifest": "<hash>", "policy": "<hash>",
    "builds": { "baseline": "<build id>", "evolved": "<build id>" }
  },
  "reused": { "baseline": "store key or cache key this stage read, if any" },
  "gate": "improved | aa | closed (quality only)",
  "reason": "only for skipped/failed",
  "workers": 12
}
```

### 5.2 Rules every stage follows

1. **Take a shared session lifecycle lock, then the stage lock**
   `stages/<stage>/lock` (exclusive `flock`). Check `clean.json` and any existing final stage
   state while holding both locks. For stages after `compile`, load `run.json` and check the
   manifest, policy and coordinator
   hashes, exactly as `--resume` does today (`Comparison.__init__`). A harness change between
   `compile` and a stage is refused, and the message names the archived harness wheel. Take
   the store from `run.json:store` and check that it and the baseline build exist. If they
   don't, exit 41. A cleaned session exits 41. `compile` creates or resumes `run.json`
   under these same locks, before later stages can read it.
2. **Check prerequisites.** Each required upstream stage must be `complete`. Otherwise exit 41
   with a message that says which stage to run.
3. **Check the gate** (gated stages only). If it is `closed`, write `skipped` with the
   reason and exit 0. `cost` also checks committed correctness evidence here and records
   `skipped` if correctness found a failure, before requiring a quiet compatible host.
4. **Check the host** (§7). The OS, architecture and Python version must match the build
   identity. Otherwise exit 41.
5. **Write `status: running`** after preconditions pass. The stage lock taken in step 1
   prevents two invocations from writing `skipped` or doing the work at once.
6. **Do the work.** Existing resume behaviour is kept:
   - quality skips seeds already in `observations.jsonl`;
   - cost reuses valid regime bundles through `validate_bundle`;
   - correctness restarts its C1–C5/C7 work, but most verifier jobs hit
     `verifier-cache/`. Before restarting, truncate its `correctness.jsonl` and
     `clifford.jsonl` and reset its stage evidence, so repeated checks do not accumulate
     duplicate rows. `unit-tests` likewise replaces its stage-owned results on retry.
7. **Commit the stage evidence before `complete`.** Write evidence and the stage output
   files atomically where possible; `state.json: complete` is the final commit marker.
   A crash writes `harness/error/<stage>` and `failed` under the stage lock. Release the
   lifecycle lock only after the final state is durable. `decide` reads full stage evidence
   only when the final state is `complete`; for `failed` it reads only the stage's harness
   error, and a `skipped` stage contributes no evidence.
   On a successful retry, remove that stage's earlier `harness/error/<stage>` before
   committing `complete`; otherwise a recovered stage would still give `ERROR`.

`decide` also holds a shared lifecycle lock while it takes a consistent snapshot of all
stage states and committed evidence, and serializes its own rewrites with a `decide` lock.
It records the hashes of the stage states it read in `decision.json`. `clean` takes the
exclusive lifecycle lock before checking those hashes and deleting bulk, so a new stage
cannot start between its check and deletion. A precondition failure before a stage can
create its state is reported as `INCONCLUSIVE`; a crash after the stage starts is `failed`
with `harness/error/<stage>` and is reported as `ERROR`.

**A stage's exit status reports whether the stage ran, not what it found.** A correctness
mismatch or a cost breach is a finding. It goes into evidence, the stage is `complete`, and
the exit status is 0. Only `decide` turns findings into a verdict and a verdict exit code.

| Exit | Meaning |
| ---: | --- |
| 0 | `complete` or `skipped` |
| 40 | Harness, build or worker error; the stage is `failed` (same as `ERROR` today) |
| 41 | *New:* precondition not met (missing or failed upstream stage, a cleaned session, a missing store entry, or the wrong host) |
| 64 | Usage error |

With these exit codes, `done(...)` in LSF means "this stage produced usable evidence or
decided it had nothing to do".

### 5.3 Running a stage again

- **`complete` or `skipped`:** the stage prints `already complete` and exits 0. It never
  runs again in this session. This makes it safe to resubmit a whole LSF chain.
- **`running` (after a killed job) or `failed`:** the stage resumes from where it stopped.
  The same holds for `compile`.

To measure a finished stage again, start a new session. With the store, the new session
reuses the baseline and pays only for the evolved build and the evolved half of each stage.

Because a finished stage never changes, a later stage can never have consumed evidence that
was replaced afterwards. There is no staleness to track.

## 6. Decision semantics

The verdict function does not change. Running quality first, and the rest only through the
gate, changes which evidence exists in these cases:

| Situation | `compare` today | After this plan |
| --- | --- | --- |
| No improvement, correct candidate | `NO_IMPROVEMENT` | `NO_IMPROVEMENT` |
| No improvement, **incorrect** candidate | `CONSTRAINT_VIOLATION` (correctness ran first) | **`NO_IMPROVEMENT`** (correctness never ran). The candidate is rejected either way. The report says "correctness not checked: no improvement" |
| Quality failure (C0, C6, guard) | `CONSTRAINT_VIOLATION`, after the full correctness suite | `CONSTRAINT_VIOLATION`, without the correctness suite |
| Improvement, correctness fails | `CONSTRAINT_VIOLATION` | `CONSTRAINT_VIOLATION`; `cost` skipped |
| Improvement, everything passes | `PASS` if cost passes | same |
| Baseline fails C1–C5, candidate improved | `INCONCLUSIVE`; quality never ran | `correctness` records `baseline/preflight` failed. `decide` keeps only the evidence from `compile` and `correctness` and sets aside the rest, so the verdict is `INCONCLUSIVE` as today. The report notes that the quality evidence was set aside |
| Baseline fails C1–C5, **no improvement** | `INCONCLUSIVE` | **`NO_IMPROVEMENT`**: the broken baseline is not seen. The baseline's correctness results are cached per baseline in the store (§10.2), so a broken baseline is found by the first session that gets through the gate, and every later session's report carries it. The report says whether the baseline's correctness is known from the store |

`*1/stage-coverage` moves from `quality` to `decide`, because it needs both quality checks and
`clifford.jsonl`. Required IDs change in two places: `harness/qualification` is removed
(§9.1), and `CA1/upstream` becomes conditional (§6.1). Record kinds and result vocabulary do
not change.

### 6.1 Optional unit tests

Today `CA1/upstream` is a fixed required ID on the confirm profile, and the iterations profile
never runs the upstream tests. After this change:

| `unit-tests` stage | `*1/upstream` required? | Effect on the verdict |
| --- | --- | --- |
| Never started (no `stages/unit-tests/`), or `skipped` by the gate | No | Decided without it. The report says "Upstream tests: not run" |
| `complete` | Yes | Regressions give `CONSTRAINT_VIOLATION`; unresolved suites give `INCONCLUSIVE`, as today |
| `running` (started, not finished) | Yes | `INCONCLUSIVE`. A started suite cannot be silently dropped by running `decide` early. Run `unit-tests` again to resume it |
| `failed` | Yes | `ERROR` from `harness/error/unit-tests`; run `unit-tests` again to resume it |

The rule is the same on both profiles: **if you run it, it counts.** `CA1/upstream` is removed
from `profiles/confirm-profile/policy.json:required_ids` in the same version-5 bump as §9.1.
`decide` adds `{prefix}1/upstream` to the required set when `unit-tests` is `complete`,
`running` or `failed`. Once a verdict has been written, starting optional `unit-tests`
invalidates that verdict until `decide` runs again; `clean` checks this via the stage-state
hashes (§5.2).

## 7. Running across several hosts

### 7.1 Worker count on shared nodes

`Comparison.workers` is currently `min(12, cpu_count - 1)`. On a busy LSF node that
oversubscribes the node. New rule: `LSB_DJOB_NUMPROC` (the slots LSF granted) when it is set,
otherwise today's default. There is no command-line override. The number used is recorded in
`state.json`.

`QUALITY_BATCHES_IN_FLIGHT` stays a cap on top of this. `cost` ignores it: it is serial by
design.

### 7.2 Host requirements

The session directory **and the store** must be on a **shared filesystem mounted at the same
path on every node**. Build environments are virtualenvs with absolute paths
(`build["python"]`, `build["environment"]`, `verify_build`), the baseline build lives in the
store, job files hold absolute paths, and `run.json:store` is an absolute path.

| Concern | Today | Change |
| --- | --- | --- |
| Build portability | Implicit: same host | Each stage checks that `platform.system()`, `platform.machine()` and the Python version match `build["identity"]`, and that `build["python"]` exists and runs. If not, exit 41 |
| Python interpreter | The build venv links to the `uv` Python that ran `compile` | Document: the `uv` Python directory (usually `~/.local/share/uv/python`) must be shared, or the same path must exist on every node. This includes the node that first built a baseline into the store |
| `run.json:machine` | Written once; used by the quality cache key and cost bundles | Keep it as the `compile` host, for information. Each stage records its own `machine` in `state.json`. The baseline quality key uses the **quality stage host's** CPU. `collect_panel` records the **cost host's** machine |
| Runner lock | `tempfile.gettempdir()/qtb-runner-<uid>.lock` | LSF often sets a per-job `TMPDIR`, which would silently make this lock per job. Use a fixed `/tmp` path, overridable with `QTB_RUNNER_LOCK`. On LSF, cost exclusivity comes from `bsub -x`. The lock and `_wait_until_quiet` remain a second line of defence |
| Cargo registry | One registry shared by all runs under `build-cache/cargo-registry`, under a lock | Each build keeps its crates in its own `cargo/` (store entry for the baseline, session for the evolved build). No cross-session lock. The evolved build downloads its crates once per session |
| Mixed CPU models on quality nodes | n/a | Baseline and evolved quality are compared only when both come from the same CPU model: the baseline quality key includes the CPU, so a session on another model recomputes the baseline half |

## 8. LSF usage (example, not shipped as harness code)

`tools/lsf/submit.sh`, for reference only. The script picks the session directory, so the
whole chain is submitted at once.

```bash
Q="uv run qiskit-transpile-bench"
STORE=/shared/qtb-store                     # one per site; holds the baselines
R=/shared/qtb-sessions/$NAME                # this session; must not exist yet
LOGS=/shared/qtb-sessions/lsf; mkdir -p "$LOGS"
O="-o $LOGS/$NAME.%J.out"

bsub -J "b-$NAME" -n 16 $O $Q compile --baseline "$B" --evolved "$E" \
     --store "$STORE" --profile confirm-profile --results-root "$R"
bsub -J "q-$NAME" -n 12 -ti -w "done(b-$NAME)" $O $Q quality     --results-root "$R"
bsub -J "c-$NAME" -n 12 -ti -w "done(q-$NAME)" $O $Q correctness --results-root "$R"
bsub -J "u-$NAME" -n 8  -ti -w "done(q-$NAME)" $O $Q unit-tests  --results-root "$R"   # optional
bsub -J "k-$NAME" -n 1 -ti -w "done(c-$NAME)" $O \
     tools/lsf/cost_if_gated.sh "$R" "$COST_MODEL"
bsub -J "d-$NAME" -n 1 \
     -w "ended(b-$NAME) && ended(q-$NAME) && ended(c-$NAME) && ended(u-$NAME) && ended(k-$NAME)" \
     $O $Q decide --results-root "$R"
```

- **When the gate is closed,** `correctness` and `unit-tests` start, record `skipped` and
  exit 0 within seconds. The lightweight `cost_if_gated.sh` job invokes `cost` locally to
  record `skipped`, then exits. It also does this when correctness found a failure. Otherwise
  it submits `cost` on an exclusive host with `-x` and `-R "select[model==$COST_MODEL]"`,
  waits for that child job, and propagates its exit status. Its own `ended()` state therefore
  covers the actual cost job. The helper must verify the child job reached a terminal state
  before it exits, including submission failures.
- **Without unit tests**, drop the `u-` job and `ended(u-$NAME)` from `decide`'s dependency.
- **If `compile` fails after creating the session**, `-ti` ends dependent stages. `decide`
  still runs and records the failure from `harness/error/compile`. A usage error before
  session creation has no session for `decide` to read; the submit script reports that job's
  exit and log instead.
- **`-x`** gives `cost` an exclusive host. **`-ti`** ends a job whose dependency can never be
  met. Without it the job would stay pending forever, and `decide`, which waits on `ended()`,
  would never start.
- **`-R select[model==…]`** pins cost to one host model. The fixed cost thresholds were set on
  one machine type.
- **Walltime (`-W`) guidance** comes from the README's timing table.

## 9. Removals

### 9.1 Qualification

Qualification was a maintainer-written attestation file (`results/qualifications/<hash>.json`)
tied to one harness version, one profile version and one machine. Without it, `PASS` was
unreachable. It is removed completely. After this change a session can end `PASS` when every
other required record passes.

| Where | Change |
| --- | --- |
| `src/qtb/coordinator/__init__.py` | Delete the qualification block in `execute` and `run["qualification"]` |
| `profiles/*/policy.json` | Remove `harness/qualification` from `required_ids` and delete the `qualification` object |
| `profiles/*/manifest.json` | Remove `"status": "unqualified"` |
| Schemas in `src/qtb/config/` | Stop requiring or accepting the removed fields, if they validate them |
| `tools/curate/curate.py`, `tools/freeze_timeouts.py` | Stop emitting `harness/qualification`, `qualification` and `status` |
| `tests/test_freeze_timeouts.py` | Update the expected draft |
| `src/qtb/reporter/` | Remove any qualification wording |

**Profile version bump, 4 → 5, for both profiles.** `required_ids` is a constraint, and
`docs/versioning.md` requires a new version when constraints change. The baseline quality key
covers the case definition and the quality protocol, not the policy hash, so the bump does not
by itself invalidate stored baseline quality.

The **A/A run and known-outcome tests stay useful as practices**. Only the attestation file
and the required record go away.

Documentation to update: `README.md` (the "Qualification is still required for `PASS`"
paragraph, the `INCONCLUSIVE` row), `docs/metrics.md` §8, `docs/output-format.md` (exit 30
wording, `qualifications/`, example records), `docs/architecture.md` (caches table),
`docs/iterations-profile.md` and `docs/confirm-profile.md` ("marked `unqualified`", required
IDs), `docs/versioning.md` ("and fresh qualification"), `docs/implementation-status.md`
(rewrite the qualification sections as "validation on a new runner"),
`docs/known-outcome-validation.md`, `fixtures/PROVENANCE.md`.

### 9.2 `compare` and `smoke`

`compare` is replaced by the stages. There is no one-command run. A shell loop or the LSF
script runs the chain. `smoke` (build, round-trip, one seed per case) is covered by `compile`,
which also leaves reusable builds behind.

| Where | Change |
| --- | --- |
| `src/qtb/cli.py` | Remove the `compare` and `smoke` subcommands, `--resume` and the `smoke` exit-code branch |
| `src/qtb/coordinator/__init__.py` | `execute` is split into the stage functions and then deleted. Remove the `smoke` parameters, the `qtb-smoke/1` result and `smoke.json` |
| `tests/test_cli.py` | Replace `compare`/`smoke` cases with the stage commands |
| `README.md` | Quick start becomes the stage chain. Remove "The smoke test" and the `compare`/`smoke` rows. "Whole `smoke` run, first time" in the timing table becomes "Whole `compile`, first time" |
| `docs/architecture.md`, `docs/environments.md`, `docs/output-format.md`, `docs/implementation-status.md`, `docs/iterations-profile.md`, `docs/confirm-profile.md`, `docs/fable-issues-resolution.md` | Replace `compare` with the stages; remove `smoke` and `smoke.json` |

### 9.3 `--change-scope`

The change scope is always inferred from the snapshot diff (`evaluator/scope.py`). A path the
mapping does not know already counts as all stages, so dropping the declaration cannot make a
change look smaller than the files say.

| Where | Change |
| --- | --- |
| `src/qtb/cli.py` | Remove the flag |
| `src/qtb/coordinator/__init__.py` | Remove the `change_scope` parameter and `self.declaration` |
| `src/qtb/evaluator/scope.py` | Remove the `declaration` parameter of `changed_scope` |
| `docs/metrics.md` §7 item 3, `README.md` options table | Remove |

### 9.4 `evaluate`

`evaluate --run` recomputed the verdict of an archived run. `decide` does this for a session,
and can be run again at any time, also after `clean` (§11).

| Where | Change |
| --- | --- |
| `src/qtb/cli.py` | Remove the subcommand |
| `src/qtb/reevaluate.py`, `tests/test_reevaluate.py` | Delete |
| `tests/test_profiles_evaluation.py` | Move the cases that use `evaluate_run` to `decide` |
| `docs/output-format.md` | Remove `reevaluations/` |
| `README.md`, `docs/architecture.md`, `docs/versioning.md`, `docs/fable-issues-resolution.md` | Remove the command |

### 9.5 `decision-counts.json`

It counted decisions per manifest hash across one results root. With one session per results
root, the count is always zero. The baseline store holds only baseline data, so it does not
belong there either.

| Where | Change |
| --- | --- |
| `src/qtb/coordinator/storage.py` | Delete `register_decision` |
| `src/qtb/coordinator/__init__.py` | Remove `run["decisions_before"]` |
| `src/qtb/reporter/__init__.py` | Remove `decisions_before` from `decision.json` and the report line |
| `tests/test_worker_storage.py` | Remove its test |
| `docs/output-format.md`, `docs/architecture.md` | Remove the field and the file |

### 9.6 Caches under the results root

| Today | After |
| --- | --- |
| `results/build-cache/baseline/<identity>/` | `STORE/wheels/<identity>/` (§10.2) |
| `results/build-cache/evolved/<identity>/` | **Removed.** Each session compiles its evolved tree once |
| `results/build-cache/cargo-registry/` | **Removed.** Each build keeps its own crates (§7.2) |
| `results/quality-cache/` | Baseline rows: `STORE/quality/<key>/`. Evolved rows: not cached across sessions. `observations.jsonl` already resumes within a session |
| `results/verifier-cache/` | `RESULTS_ROOT/verifier-cache/`, per session |

Existing caches are not migrated. The first session against a baseline fills the store.

## 10. The baseline store

### 10.1 What a new session reuses

| Baseline work | Today, on a new run | After this plan |
| --- | --- | --- |
| Qiskit build | The wheel cache skips the Rust compile. The snapshot copy, venv and pip installs still run: a few minutes | **Reused from the store: no work** |
| Quality | Reused through `quality-cache/` | **Reused from the store** (same key as today) |
| Correctness: C1–C5 (2,400 compiles plus API checks) and C7 | Recompiled; only the verifier results are cached | **Reused from the store** |
| Unit tests: the pytest suite and `cargo test` | Rerun in full | **Reused from the store** |
| Determinism audit | Recompiles a sample of both revisions | Unchanged. It is the check that stored baseline rows are still reproducible |
| Cost | Both arms measured, interleaved | Unchanged. Cost samples must come from one session with both arms, so they are never stored |

### 10.2 Layout and keys

```text
STORE/
  builds/<key>/          READY, build.json, env/, source/, cargo/, wheels/, build.log
  builds/<key>.lock      held while that key is being built
  wheels/<identity>/     baseline Qiskit wheel (today's build-cache/baseline/)
  quality/<key>/         baseline quality observations and their output files
  correctness/<key>/     rows.jsonl, evidence.json, provenance.json
  unit-tests/<key>/      python.json, rust.json, logs, provenance.json
```

| Entry | Key covers | Written when |
| --- | --- | --- |
| `builds/` | Build identity (snapshot tree hash, Python, lock files, Rust toolchain, native compilers, flags, OS, architecture) and the harness wheel hash, which is installed in the venv | `compile` built it and `verify_build` passed |
| `wheels/` | Build identity, as today | The Rust compile finished. A harness change gives a new `builds/` key but the same wheel, so it costs a venv and pip install, not a Rust compile |
| `quality/` | Today's `quality_cache_key`: build ID, case, CPU model, worker protocol, harness, measurement protocol, seed block | per seed, as today |
| `correctness/` | Build ID, implementation identity, harness hash, correctness-suite hash, full manifest and policy hashes (including C7 case definitions and referenced artifact hashes), verifier identity, CPU model, worker environment | every baseline check is decisive (`verified` or `mismatch`) and no worker timed out |
| `unit-tests/` | Build ID, harness hash, `dev-tests.lock` hash, the baseline test tree hash, test budgets, CPU model | both baseline suites `completed` |

The `correctness/` entry holds the baseline rows of `correctness.jsonl` and `clifford.jsonl`
and the evidence `behavior/baseline/*`, `api/baseline/*`, `C7/baseline/*`,
`*1/C1-C5/baseline` and `baseline/preflight`. The `unit-tests/` entry holds the baseline
`python_suite` and `rust_suite` result dicts (`completed`, `failed`, `passed`) and their logs.
Store-owned result paths (`records`, `log`) are rewritten to files inside the entry before
publication. A later session never follows a path into the session that populated it.

### 10.3 How `compile` finds an earlier baseline build

There is no separate build registry, and a session never points at another session.
`compile` recomputes the key from its current inputs and checks whether
`STORE/builds/<key>/READY` exists. The same inputs give the same key, so the entry is found
again. Any changed input gives a new key, and so a miss. The Qiskit wheel cache already works this way
(`build_revision`, `digest(identity)`).

1. Snapshot the baseline into the session, as today. The snapshot gives the tree hash and,
   with the evolved snapshot, the diff for scope.
2. Resolve the toolchain and compiler versions. This is cheap: `rustc -Vv`, `cc --version`.
3. Compute the key. `build_revision` is split into `build_identity()` and
   `build_into(destination)` so this happens before any build work. Today the identity is
   computed after the destination is created and the source copied, too late to skip that
   work.
4. **Hit, and `verify_build` passes:** record `run.json:builds.baseline` with the store paths
   and `"reused": true`. The log says "Reusing baseline build <key> from the store". Before
   accepting the hit, verify that `build.json` and its `snapshot.path` point inside this
   store entry, including the stored source tree; no build metadata may point into the
   session that first populated the store.
5. **Miss:** take `<key>.lock`, recheck `READY`, and build directly at the stable final path
   `STORE/builds/<key>/`. A Python virtual environment and installed scripts contain
   absolute paths, so building under a `.partial-*` name and renaming it would break them.
   The stored `snapshot.path` must be `<key>/source`, with the original snapshot hash and
   source provenance retained. After `verify_build` passes, atomically write `READY` last.
   Readers ignore any directory without `READY`; a retry under the key lock replaces or
   repairs that incomplete directory before publishing it. A second session waits on the
   lock, then rechecks and reuses the ready entry. The evolved build still uses its
   session-local snapshot.

The evolved build is built in the session at the same time, as today.

**A ready build entry is immutable.** `unit-tests` reads `<key>/source` but sets a
session-local `CARGO_HOME` and `CARGO_TARGET_DIR` for `cargo test`; it must not run the
current `python_suite` pip install against the stored `env/`. The required Python test
dependencies are already installed by `build_revision` from `dev-tests.lock`. Run pytest
with bytecode writing disabled and copy the test tree into the session as today. A store
build can then be checked byte for byte before and after `unit-tests`. Disable bytecode
writing for every stage process that imports Python modules from a ready stored build.

### 10.4 Reading and writing the baseline results

- **On a hit,** the stage copies the stored rows and records into the session, marks each with
  `cached_from: <store key>`, and runs only the evolved half. The aggregate records
  (`*1/C1-C5`, `*1/C7`, `*1/upstream`, the quality panels) are computed exactly as today from
  the stored baseline half and the fresh evolved half. `judge()` in `upstream.py` needs only
  the baseline dicts.
- **On a miss,** the stage runs the baseline half and writes it to the store when the
  "written when" condition in §10.2 holds.
- **A/A sessions bypass the baseline results** (same build ID for both revisions), as
  `quality-cache/` does today, so an A/A session stays a fully independent check. It still
  reuses the baseline build.
- **A suspect entry is deleted by hand** (§10.6). The next session that needs it recomputes
  it. A failed determinism audit invalidates the matching baseline quality keys under their
  per-key locks; readers must check invalidation before reuse. It does not invalidate
  correctness or unit-test results, which the quality audit did not test. Do not unlink an
  entry while another stage can be reading it.
- **The report says what was reused,** one line per stage, for example "baseline correctness:
  from the store (key 3f9a…, first computed 2026-09-25)".

### 10.5 The workflow

```bash
export QTB_STORE=/shared/qtb-store      # or pass --store to every compile

# Session 1: builds and measures both sides; fills the store with the baseline
Q="uv run qiskit-transpile-bench"
$Q compile --baseline ~/qiskit-main --evolved ~/qiskit-idea1 --store "$QTB_STORE" --results-root ~/sessions/idea1
$Q quality --results-root ~/sessions/idea1
$Q correctness --results-root ~/sessions/idea1     # skipped unless quality improved
$Q cost        --results-root ~/sessions/idea1     # skipped unless correctness passed too
$Q decide      --results-root ~/sessions/idea1
$Q clean       --results-root ~/sessions/idea1

# Session 2, any time later: same baseline tree, new evolved tree, new session directory
$Q compile --baseline ~/qiskit-main --evolved ~/qiskit-idea2 --store "$QTB_STORE" --results-root ~/sessions/idea2
#   "Reusing baseline build <key> from the store"; only idea2 is built
$Q quality --results-root ~/sessions/idea2
#   baseline seeds from the store; only idea2 is compiled
#   ... and so on; correctness and unit-tests also read the baseline half from the store
```

The baseline is reused only when **all** of these hold. Otherwise that part is rebuilt or
recomputed, and the progress log says why:

- **Same store.** Pass the same `--store` (or `QTB_STORE`).
- **Same baseline content.** The tree hash of the snapshot counts, not the folder path. A new
  commit or any edited file in the baseline folder is a new baseline.
- **Same harness version, lock files, Rust toolchain and Python.** The harness wheel hash is
  in every key, so any edit to the harness code gives a new baseline venv (not a new Rust
  compile) and recomputes the baseline quality, correctness and unit-test results. While you
  are changing the harness, expect no baseline reuse.
- **Same CPU model** for the baseline quality, correctness and unit-test results. The build
  itself depends only on the OS and architecture.

### 10.6 Maintaining the store

There is no command for it. Ready build, correctness and unit-test entries are only added;
quality entries can be invalidated after a failed audit (§10.4).

- **Size.** About 330 MB per baseline build once `target/release` is removed (§11), plus a few
  MB of results per baseline. `du -sh $STORE/*` shows it.
- **Deleting.** Delete an entry, or the whole store, when no session is running against it.
  Every entry can be recomputed, so nothing is lost but time. A session whose baseline build
  was deleted cannot run more stages (exit 41, naming the key). Its `decide` still works,
  because the evidence is in the session.
- **Leftovers.** A `builds/<key>/` directory without `READY` is an interrupted build. A
  later `compile` repairs it under `<key>.lock`; an operator can remove it when that lock is
  not held and no session is using it.

## 11. Disk cleanup

Measured on the 2026-09-24 confirm A/A run (8.3 GB):

| Item | Size | Needed later? |
| --- | ---: | --- |
| `source/target/debug`, from `cargo test` | 1.9 GB per build | No, once the unit tests are done |
| `source/target/release`, from the wheel build | 1.0 GB per build | No. The wheel is installed in `env/`, and `cargo test` uses the debug profile |
| `env/` | 312 MB per build | Yes, while stages still run |
| `upstream-*/tests.jsonl` | 308 MB each (3 per run) | Only the failures |
| `verifier/` | 291 MB per session | Yes, while stages still run |

**Automatic, by the stage that made it:**

| Cleanup | Done by | When |
| --- | --- | --- |
| `source/target/release` | `compile` | right after the wheel is installed and `verify_build` passes, for both builds. A store build gets its `READY` marker only after this |
| Rust test build | `unit-tests` | `CARGO_TARGET_DIR` points at `upstream-<rev>/target` (or `$TMPDIR` on LSF) and is deleted when the stage ends |
| `tests.jsonl` | `unit-tests` | failure text kept only for failed tests; the file is gzipped |

**Explicit: `clean --results-root DIR`.** It works on one session directory and never touches
the store.

- **Deletes:** the evolved build (`builds/evolved-build/`), the snapshots (`builds/<rev>/`),
  `verifier/`, `verifier-cache/`, `jobs/*/scratch` and `out/` files, `oracle-jobs/`, the
  copied `upstream-*/test` trees, and large verified outputs (today's `prune_outputs` and
  `prune_prefix_outputs`, with the policy's `output_retention_bytes`).
- **Keeps:** `run.json`, `manifest.json`, `policy.json`, `harness-wheel/`, `stages/`,
  `evidence.json`, `decision.json`, `report.md`, the logs, `observations.jsonl`,
  `correctness.jsonl`, `clifford.jsonl`, `cost/`, `changed-tests.json`, every `job.json` and
  the snapshot manifests (`builds/<rev>.snapshot.json`).
- **Records** every planned deletion in `clean.json` with the file's hash before unlinking,
  as `retention.json` does today. It writes `status: cleaning` before deleting anything and
  `status: complete` after the last deletion, then prints the space freed. A killed `clean`
  leaves the `cleaning` marker; a later `clean` resumes idempotently from the recorded list.
- **Takes the exclusive lifecycle lock** (§5.2) before checking stage states or deleting
  anything. It refuses (exit 41) if a stage is `running`, or if the stage-state hashes differ
  from those saved by the latest `decide`. The verdict of the session is therefore always
  written after its last stage transition and before its bulk goes. A `running` state left
  by a killed job must first be resumed to a final state; a stale lock file alone is not a
  reason to block cleanup.
- **Is final.** After `clean`, `decide` still works (it reads only kept files), but every
  other stage exits 41 with "session cleaned". The same applies while `clean.json` says
  `cleaning`; resume `clean` first. To measure again, start a new session. With the
  store, that costs only the evolved build.
- **Ignores the verdict.** Today's pruning skipped runs that did not end `PASS` or
  `NO_IMPROVEMENT`, to keep them for investigation. With an explicit command, you choose when
  to run it. `decide` no longer deletes anything.

A cleaned session shrinks to a few MB. To remove a session entirely, delete its directory.

## 12. Phases

### Phase 1: removals that do not need the split

1. **Remove qualification** (§9.1), with the profile version bump. Make `CA1/upstream`
   conditional (§6.1) in the same bump.
2. **Remove `smoke`, `evaluate`, `--change-scope` and `decision-counts.json`** (§9.2–9.5).
   `compare` stays until Phase 2.
3. **Record golden verdicts.** Run `compare` on every test fixture and keep each
   `decision.json` as a golden file for Phase 2 (§13). Doing this after the removals means
   the goldens already reflect the removals, so they differ from the staged results only
   where §6 says they should.

### Phase 2: stages replace `compare`

1. **`qtb/coordinator/stages.py`** (new): `StageContext` with open, prerequisites, gate check,
   host check, the lifecycle and stage locks, `state.json`, evidence and progress paths. The
   `STAGES` graph with each stage's prerequisites and owned outputs.
2. **`Comparison`:**
   - split `__init__` into `create` (for `compile`, with the results root as the session
     directory) and `open(results_root, stage)`;
   - `evidence()` writes through the stage context;
   - `workers` follows §7.1; `run.json` remains compile-owned (§5).
3. **`aggregate_checks`:** move stage coverage into a separate `stage_coverage()` that
   `decide` calls.
4. **Stage functions:** `compile_stage` (today's `build` plus `roundtrip`), `quality_stage`
   (ends with the gate, §4.4), `correctness_stage`, `unit_tests_stage`, `cost_stage`, and
   `decide` (`_finish` without pruning or the decision count, plus the evidence merge, the §6
   and §6.1 rules and the stage table in the report). Delete `execute`.
5. **`cli.py`:** the new subcommands, each with only `--results-root`, and exit code 41.
   Remove `compare`.
6. **`clean`** (§11), reusing `prune_outputs` and writing `clean.json`. Move the other
   automatic cleanups of §11 into `compile` and `unit-tests`.
7. **`costs.py`:** bundles record the current host's machine; the runner lock path follows
   §7.2. **`storage.py`:** `quality_cache_key` takes the current host's machine.

In Phase 2 the caches stay where they are, under the session. `--store` is accepted and
recorded but not yet used.

### Phase 3: the baseline store (§10)

1. **`--store`** required on `compile`, recorded in `run.json:store`.
2. **Baseline build:** split `build_revision` into `build_identity()` and `build_into()`;
   store lookup, key lock, stable final-path build and atomic `READY` marker in
   `compile_stage`; the baseline wheel cache
   moves to `STORE/wheels/`. Remove the evolved wheel cache and the shared cargo registry.
3. **Baseline results:** baseline quality moves to `STORE/quality/`; add
   `baseline_correctness_key`, `baseline_unit_tests_key` and their read and write helpers in
   `storage.py`; hit and miss handling in `quality`, `behavior_checks`, `clifford_checks` and
   `upstream_checks`; the audit-failure purge extended to the new entries.
4. **Report:** a line per stage listing what was reused.
5. **`verifier-cache/`** moves into the session.

### Phase 4: cluster helpers

- `tools/lsf/submit.sh` and `cost_if_gated.sh` (§8), including the wrapper that submits
  `cost` only when the gate is open and correctness passed;
- documentation in `docs/environments.md` on shared-filesystem requirements.

## 13. Tests

All of these use the existing mocked-worker fixtures in `tests/`, so they need no Qiskit
build.

- **Golden verdicts:** each fixture run through `compile → quality → correctness →
  [unit-tests] → cost → decide` gives the golden `decision.json` from Phase 1, after
  normalizing timestamps and the stage block. The two expected
  differences in §6 (an incorrect candidate with no improvement, and a broken baseline with no
  improvement) are separate fixtures that assert `NO_IMPROVEMENT` and the report line.
- **Gate:**
  - no improvement: `correctness`, `unit-tests` and `cost` record `skipped` and exit 0, and
    `decide` gives `NO_IMPROVEMENT`;
  - a quality failure closes the gate, and the verdict is `CONSTRAINT_VIOLATION`;
  - an unresolved audit, C0, C6 or C1-lite closes the gate, including on A/A runs;
  - an A/A session runs every stage;
  - `cost` skips when correctness failed.
- **Concurrency:** `correctness` and `unit-tests` run in two processes at once on one session.
  Both stage evidence files are complete, and `decide` merges them with no lost records.
  Calling `decide` during either stage ignores partial evidence and gives `INCONCLUSIVE`.
- **Preconditions:** each stage exits 41 before its prerequisites are complete, after `clean`,
  and when the store entry is missing.
- **Running again:** a `complete` stage prints `already complete`, exits 0 and changes
  nothing; a `failed` or `running` stage resumes without rebuilding. Retried correctness
  has no duplicate JSONL rows, and a recovered stage no longer contributes its old
  `harness/error/<stage>`.
- **Session directory:** `compile` on an existing session with other arguments exits 64; on a
  non-session directory exits 64; on its own unfinished session resumes; on a finished one
  prints `already complete`.
- **Optional unit tests on confirm:**
  - never started or gate-skipped → `*1/upstream` not required, and a passing session is
    `PASS`;
  - complete with a regression → `CONSTRAINT_VIOLATION`;
  - `running` → `INCONCLUSIVE`, `failed` → `ERROR`.
- **`decide` with missing stages:** `INCONCLUSIVE`, with the missing stages named in the
  report's stage table.
- **Store:**
  - `compile` with neither `--store` nor `QTB_STORE` exits 64, and `--store` wins over
    `QTB_STORE`;
  - a store path that does not exist exits 64 and creates nothing;
  - a second session with the same baseline tree reuses the build and builds only the evolved
    tree;
  - two sessions racing on one key build it once; a killed build has no `READY` marker and
    is repaired under the key lock;
  - after cleaning the first session, the second session's stored baseline interpreter,
    source snapshot, test runner and build metadata still work and point only into the store;
  - the second session replays baseline quality, correctness and unit-test results, marked
    `cached_from`, and gives the same records as a fresh run;
  - A/A bypasses the stored baseline results but reuses the build;
  - a deleted store entry is recomputed by the next session;
  - an `unverified` baseline check is never stored;
  - no evolved data is ever written to the store;
  - the store entry is byte-identical before and after `unit-tests`.
- **Clean:**
  - refused while a stage runs, and before `decide`; a stage cannot start after `clean`
    takes the lifecycle lock;
  - refused when optional `unit-tests` starts after a prior `decide`, until `decide` reruns;
  - a killed cleanup leaves `clean.json: cleaning`; later stages exit 41 and rerunning
    `clean` finishes the recorded deletions;
  - after `clean`, `decide` reproduces the same verdict, and other stages exit 41;
  - `clean` never touches the store.
- **Host check:** a build identity with a different architecture gives exit 41.
- **Workers:** `LSB_DJOB_NUMPROC=3` gives 3 workers; without it, today's default.
- **Removals:** `compare`, `smoke` and `evaluate` are usage errors (exit 64); `--change-scope`,
  `--workers`, `--rerun` and `--force` are rejected; version-5 policies have no
  `harness/qualification`; a full passing fixture session ends `PASS`.

## 14. Documentation to update

In addition to §9:

| Document | Change |
| --- | --- |
| `README.md` | Quick start: the stage chain with `--store`; commands table; "One session per results root"; "Reusing a baseline" (same store; harness edits break reuse); the gate and what `NO_IMPROVEMENT` does and does not check; `clean`; LSF pointer; unit tests optional |
| `docs/architecture.md` | "Lifecycle of `compare`" becomes the stage graph with the gate; process model across hosts; session directory vs store; the caches table becomes the store layout |
| `docs/output-format.md` | The session layout (no `runs/`), `stages/`, `state.json`, per-stage evidence and progress, `cached_from`, `run.json:store`, stage summary and hashes in `decision.json`, `clean.json` in place of `retention.json` |
| `docs/metrics.md` | Cost section: when cost runs now follows the gate; §7 without the scope declaration |
| `docs/environments.md` | Shared-filesystem requirements for sessions and the store, `QTB_STORE`, the interpreter path, the runner lock, disk use, the cleanup rules and store maintenance |
| `docs/confirm-profile.md`, `docs/verifier.md` | Upstream tests are optional; the "if you run it, it counts" rule |
| `docs/versioning.md` | `qtb-stage/1` format; profile version 5; `evaluate` removed |
