# Output: the session directory, `decision.json` and `report.md`

A session is one directory: the `--results-root` given to `compile` (default `results`). It
holds one baseline/evolved pair, the evolved build, the verifier, every stage's output and the
verdict. There is no `runs/` level; to compare another pair, pass another `--results-root`.
Every later command takes the same `--results-root`.

The baseline build and the stored baseline results live in the store, not in the session
([below](#the-store), and [environments.md](environments.md)).

Each stage prints its final state, `decide` prints the verdict, and `clean` the space freed:

```text
quality: complete (/…/sessions/idea1)
correctness: skipped: gate closed: no improvement.
PASS (iterations-profile): /…/sessions/idea1
Cleaned /…/sessions/idea1: freed 1.42 GiB in 38 items.
```

## What to open first

| You want | Open |
| --- | --- |
| The verdict and why, in words | `report.md` |
| The verdict for scripts or CI | the exit code of `decide`, or `decision.json` → `status` |
| Which stages ran, where and for how long | the "Stages" table at the top of `report.md`, or `stages/<stage>/state.json` |
| Why a stage failed or was skipped | `stages/<stage>/state.json` → `reason`, then `stages/<stage>/progress.log` |
| Why a build failed | `builds/evolved-build/build.log`, or `STORE/builds/<key>/build.log` for the baseline |
| Why one compile failed | `jobs/<id>/worker.log` and `jobs/<id>/out/results.jsonl` |

## Exit codes

A stage's exit status reports whether the stage ran, not what it found. A correctness mismatch
or a cost breach is a finding: it goes into the stage's evidence, the stage is `complete`, and
the exit status is 0. Only `decide` turns findings into a verdict.

Stages (`compile`, `quality`, `correctness`, `unit-tests`, `cost`):

| Code | Meaning |
| ---: | --- |
| 0 | The stage is `complete` or `skipped`, now or already |
| 40 | The stage failed: a harness, build or worker error. Its state is `failed` and its evidence holds `harness/error/<stage>`. Run the stage again to resume it |
| 41 | A precondition is not met: an upstream stage is missing or unfinished, the directory is not a session, the session is cleaned or its cleanup was interrupted, the harness or profile changed since `compile`, the store or its baseline build is missing, this host cannot run the builds, or the same stage is already running |
| 64 | Usage error: bad options, no store given, the store does not exist, or the results root holds another session or is not a session |

`decide` exits with the verdict:

| Code | Status | Meaning |
| ---: | --- | --- |
| 0 | `PASS` | Improvement shown and every required constraint satisfied |
| 10 | `NO_IMPROVEMENT` | Valid, complete panel, but the improvement rule was not met; nothing violated |
| 20 | `CONSTRAINT_VIOLATION` | The candidate failed a correctness check or an established quality/cost guard |
| 30 | `INCONCLUSIVE` | Something required is missing or unresolved, a required stage has not finished, or the baseline itself failed |
| 40 | `ERROR` | A stage failed (harness, build or input failure) |
| 41 | — | The directory is not a session |
| 64 | — | Command-line usage error |

`clean` exits 0 when the session is clean (also when it already was) and 41 when it refuses.

## Directory contents

| Path | Written by | Content |
| --- | --- | --- |
| `run.json` | `compile` | Session inputs, hashes, builds and store ([below](#runjson)). No other command writes it |
| `manifest.json`, `policy.json` | `compile` | Archived copies of the profile used. Later stages check them against `run.json`; `decide` decides from them |
| `harness-wheel/`, `harness-build.log` | `compile` | The harness wheel installed in every environment |
| `builds/baseline/`, `builds/evolved/`, `builds/<revision>.snapshot.json` | `compile` | Source snapshots of both trees and their file manifests ([environments.md](environments.md)) |
| `builds/evolved-build/` | `compile` | The evolved build: `source/`, `env/`, `cargo/`, `wheels/`, `build.json`, `build.log`. The baseline build is in the store |
| `verifier/` | `compile` | Verifier environment |
| `verifier-cache/` | Any stage | Verifier results, addressed by content. Only `verified` and `mismatch` results are kept; writes are atomic and an entry is re-checked before reuse |
| `observations.jsonl` | `quality` | One line per quality compile (see below) |
| `correctness.jsonl` | `correctness` | Every C0–C5 check from the correctness suite |
| `clifford.jsonl` | `correctness` | C7 results: prefix pipeline, plus the full pipeline in the confirm profile |
| `changed-tests.json` | `unit-tests` | Test files and Rust sources that the evolved tree changed |
| `upstream-baseline/`, `upstream-evolved/` | `unit-tests` | Upstream pytest records (`tests.jsonl.gz`, failure text kept only for failed tests), `tests.log`, `rust.log` and the copied `test/` tree. With a stored baseline, the baseline records and logs stay in the store entry and `upstream-baseline/` is not written |
| `upstream-evolved-own/`, `upstream-evolved-own.json` | `unit-tests` | Report-only run of the candidate's own Python tests |
| `cost/<panel>/screen.json`, `normal.json`, `rerun.json` | `cost` | Raw two-arm timing or memory bundles, one per regime reached; sessions in subdirectories. A timing session holds one `timing_batch` job per round and arm |
| `jobs/<uuid>/` | Worker | `job.json`, `worker.log`, `out/results.jsonl`, output circuits `out/output-<seed>.ops.jsonl.gz` |
| `oracle-jobs/<uuid>/` | Verifier | `job.json`, `result.json` for each distinct verifier job not already cached |
| `oracle-jobs/batch-<uuid>/` | Verifier | `batch.json` (the jobs one verifier process ran, in order) and its `verifier.log` |
| `stages/<stage>/state.json` | That stage | The stage's state ([below](#stagesstagestatejson)) |
| `stages/<stage>/evidence.json` | That stage | The constraint records the stage produced |
| `stages/<stage>/progress.log` | That stage | Timestamped progress (wall clock and elapsed time), a start/end line with the duration of every step, one line per quality batch, and the stage's table of step durations. A retried stage appends to it |
| `stages/<stage>/lock`, `stages/decide.lock` | That stage, `decide` | Locks: one invocation of a stage, or of `decide`, at a time |
| `lifecycle.lock` | Stages, `decide`, `clean` | Held shared by stages and `decide`, exclusively by `clean` |
| `evidence.json` | `decide` | The committed evidence of every stage, merged |
| `decision.json` | `decide` | Machine-readable verdict |
| `report.md` | `decide` | Human-readable verdict |
| `progress.log` | `decide` | The stage logs concatenated in stage order, then one table of step durations for all stages |
| `clean.json` | `clean` | What `clean` deleted ([below](#cleanjson)) |

Every shared file has one writer. `decide` reads a stage's evidence only when its state is
`complete`; from a `failed` stage it reads only the `harness/error/<stage>` record, and from a
`running`, `skipped` or unstarted stage nothing. A record ID that appears in two stage
evidence files is a harness error. `decide` adds `*1/stage-coverage` itself once `quality` and
`correctness` are both complete.

## `run.json`

Format `qtb-run/2`. Written only by `compile`; `decide` never rewrites it.

| Field | Content |
| --- | --- |
| `run_id` | `<YYYYMMDDTHHMMSS>-<8 hex>`, UTC creation time plus a random suffix |
| `profile` | `iterations-profile` or `confirm-profile` |
| `hashes` | Manifest, policy, coordinator, harness wheel and implementation hashes. Every later stage refuses to run if the coordinator, implementation, manifest or policy differs |
| `created_at` | When the session was created |
| `session` | The session directory, absolute |
| `store` | The store, as an absolute resolved path. Later stages and `decide` take it from here |
| `machine` | Machine identity of the `compile` host, for information. Each stage records its own host in its `state.json` |
| `sources` | The baseline and evolved folders, absolute |
| `builds` | The `build.json` of each revision. The baseline also has `store_key` and `reused` (`true` when the build came from the store) |
| `changed_paths`, `scope` | Files that differ between the snapshots, and the change scope per optimization level |
| `status` | `created`, then `built` once both builds and the verifier are ready |
| `coverage_gaps` | The profile's declared coverage gaps |
| `verifier_python` | The verifier interpreter |

## `stages/<stage>/state.json`

Format `qtb-stage/1`. `status: complete` is written last and is the stage's commit marker.

| Field | Content |
| --- | --- |
| `stage` | `compile`, `quality`, `correctness`, `unit-tests` or `cost` |
| `status` | `running`, `complete`, `skipped` or `failed`. A killed job leaves `running` |
| `started_at`, `finished_at`, `seconds` | When the latest attempt ran and how long it took |
| `attempts` | How many times the stage has started |
| `machine` | Machine identity of the host that ran it |
| `scheduler` | `LSB_JOBID`, `LSB_QUEUE`, `LSB_HOSTS`, `LSB_MCPU_HOSTS` and `LSB_DJOB_NUMPROC`, when set |
| `inputs` | Coordinator, implementation, harness, manifest and policy hashes, and the build ID of each revision |
| `workers` | Worker processes used |
| `steps` | Every step's name, depth, duration and status |
| `reused` | What came from the store: `baseline_build` (key), `baseline_quality` (list of keys), `baseline_correctness` (key) or `baseline_unit_tests` (key) |
| `gate`, `gate_reason` | `quality` only: `improved`, `aa` or `closed`, and why |
| `reason` | Why the stage was `skipped` or `failed` |
| `cost_due` | `cost` only: why cost was measured (`improved` or `aa`) |

A `complete` or `skipped` stage never runs again in its session: it prints `already complete`
(or `already skipped`) and exits 0. A `running` or `failed` stage resumes when run again. To
measure a finished stage again, start a new session.

## Replayed baseline rows (`cached_from`)

When the store already holds a baseline result, the stage copies it into the session and runs
only the evolved half. Every copied row and record carries `cached_from: <store key>`:
baseline observations in `observations.jsonl`, baseline rows in `correctness.jsonl` and
`clifford.jsonl`, the records `behavior/baseline/*`, `api/baseline/*`, `C7/baseline/*`,
`*1/C1-C5/baseline` and `baseline/preflight`, and the baseline upstream records
`*1/upstream/baseline` and `*1/upstream/rust/baseline`. Aggregate records are computed as for
a fresh run. An A/A session (identical build IDs) never replays stored baseline results.

## `decision.json`

Schema: `src/qtb/config/schemas/decision.json` (format `qtb-decision/1`).

| Field | Content |
| --- | --- |
| `status` | `PASS`, `NO_IMPROVEMENT`, `CONSTRAINT_VIOLATION`, `INCONCLUSIVE` or `ERROR` |
| `improved_under_constraints` | `true` for `PASS`, `false` for `NO_IMPROVEMENT` and `CONSTRAINT_VIOLATION`, `null` otherwise |
| `profile`, `run_id`, `seed_block` | Which profile, which session; always block `B0` |
| `hashes` | Manifest, policy, coordinator, harness wheel and implementation hashes |
| `stages` | One entry per stage: `stage`, `status` (or `not started`), `required`, `host`, `seconds`, `attempts`, `note` and `reused` |
| `stage_state_hashes` | The hash of each `state.json` this decision read (`null` for a stage not started). `clean` checks them |
| `notes` | The notes printed under the stage table in `report.md` |
| `identities` | Build ID of each revision (`baseline`, `evolved`) |
| `constraints` | Every constraint record: `id`, `kind`, `subject`, `result`, plus details such as `value`, `SE`, `threshold` and `detail` |
| `required_ids` | The record IDs that must all pass for `PASS` |
| `reasons` | Every record that did not pass: `code`, `result`, `detail` |
| `missing_records` | Required IDs with no record at all |
| `summaries` | Per-panel estimates: `ln_score`, `score`, `SE`, `ln_score_plus_2SE`, per-seed `deltas`, and per-case estimates with `worst_seed`; confirm also has `instance_bootstrap` and `leave_iterations_out` |
| `scope` | Changed stages, substituted components and unmapped paths, per optimization level |
| `cost_thresholds` | The fixed cost thresholds of the archived policy that every cost record was judged against |
| `fingerprint_changes` | Cases whose pipeline fingerprint (pass list and search budgets, seed 0) differs between revisions |
| `coverage_gaps` | The profile's declared coverage gaps |
| `measurement_timestamp` | When the session was created |

Which stages are required depends on the session: `compile` and `quality` always;
`correctness` when the gate is `improved` or `aa`; `cost` when the gate is open and
correctness found no failure; `unit-tests` once it has started. A required stage that has not
finished makes the verdict `INCONCLUSIVE` (a failed stage gives `ERROR`). When `unit-tests` is
`running`, `failed` or `complete`, `*1/upstream` is added to `required_ids`.

A constraint record looks like this:

```json
{"id": "IA2/improvement", "kind": "improvement", "result": "passed", "subject": "evolved",
 "reference": "baseline", "value": -0.0213, "SE": 0.0054, "threshold": 0.0}
```

`value` is `ln(score)`. The rule that was applied is described in [metrics.md](metrics.md#4-decision-rules).
A stage that crashed contributes `harness/error/<stage>` (kind `harness`, result `failed`),
which gives `ERROR`. A successful retry of the stage drops it.

## `report.md`

The same information as `decision.json`, written for people. Sections, in order:

1. **Title:** `# <STATUS> — <profile>`.
2. **Stages:** one row per stage with its state, host, duration and a note: the gate and its
   reason for `quality`, the reason for a skipped or failed stage, `optional` for unit tests
   never started, `not needed` for a stage the verdict does not require.
3. **Notes:** when the gate is closed, that correctness, unit tests and cost were not checked,
   and whether the baseline's correctness is known from the store; "Upstream tests: not run.";
   unfinished required stages; one line per baseline result reused from the store (for
   example "baseline correctness: from the store (key 3f9a…, first computed 2026-09-25).");
   that the quality evidence was set aside because the baseline failed its preflight; and
   that a different harness version decided than the one that compiled.
4. **Reminders:** the baseline is the reference, and confirm is a check, not a tuning loop.
5. **Cost thresholds line:** the fixed panel and per-case thresholds from the policy, with a
   reminder that they are not calibrated on this machine.
6. **Constraints needing attention:** every record that is `failed`, `unresolved` or
   `not_evaluated`, with its detail, and every required record that is missing. **Start here
   when the verdict is not `PASS`.**
7. **Quality estimates:** one row per panel: ratio (`score`; below 1 is better), paired SE in
   log units, and the upper bound `ln(score) + 2·SE`. The confirm profile adds the report-only
   instance bootstrap.
8. **Per-case changes:** each case and metric with its seed-aggregated ratio and worst seed.
9. **Compilation cost:** each cost panel's result and the candidate's log time ratio
   against the baseline.
10. **Coverage and provenance:** the stage-coverage rule, the change scope as JSON, declared
    workload gaps, and the cases whose pipeline fingerprints changed.

An illustrative excerpt (numbers and keys invented to show the layout):

```markdown
# INCONCLUSIVE — iterations-profile

## Stages

| Stage | State | Host | Duration | Note |
| --- | --- | --- | ---: | --- |
| compile | complete | node07 | 6m12s |  |
| quality | complete | node12 | 21m40s | gate improved: quality improved with every quality check passing |
| correctness | complete | node12 | 14m05s |  |
| unit-tests | not started | — | — | optional |
| cost | complete | node31 | 7m20s |  |

- Upstream tests: not run.
- baseline build: from the store (key 3f9a1c07b2e4…).
- baseline correctness: from the store (key 81d0e5aa9c3f…, first computed 2026-09-24).

## Constraints needing attention

- **unresolved** `IA1/stage-coverage`:

## Quality estimates

| Panel | Ratio | Paired SE (log) | Upper 2 SE |
| --- | ---: | ---: | ---: |
| IA2/improvement | 0.978900 | 0.005400 | -0.010526 |
| IA3/primary/N2 | 1.004100 | 0.002100 | 0.008292 |
| IA3/cx/D2 | 0.981200 | 0.005900 | -0.007179 |
```

How to read it: the `D2` score is 2.1% lower with an upper bound below zero, so the
improvement rule passed. `N2` rose 0.4%, within its 3·SE guard. The verdict is still
`INCONCLUSIVE` because the change touched a stage no verified check covers.

## Deciding again

`decide` can run any time after `compile` created the session, as often as you like, and also
after `clean`. It decides from the session's archived `manifest.json` and `policy.json`
(checked against the hashes in `run.json`) and the committed stage evidence; cost records are
recomputed from the raw bundles in `cost/`. It compiles nothing, so it also works with a
newer harness, for example after fixing an evaluator or reporting bug; the report then notes
that a different harness version decided. Each run replaces `evidence.json`, `decision.json`,
`report.md` and `progress.log`. Copy them first to keep an earlier verdict.

## `clean.json`

`clean` deletes a finished session's bulk and keeps its results. It refuses (exit 41) while a
stage is `running`, before any `decide`, or when a stage state changed since the latest
`decide` (for example, optional unit tests started afterwards: run `decide` again). It never
touches the store and ignores the verdict.

- **Deleted:** `builds/evolved-build/`, the snapshot copies `builds/baseline/` and
  `builds/evolved/` (and any `*.failed-*` build directory), `verifier/`, `verifier-cache/`,
  `oracle-jobs/`, every `jobs/**/scratch`, the copied `upstream-*/test` trees, the
  session-local `upstream-*/cargo` directories, and verified
  quality outputs and verified C6 prefix outputs larger than the policy's
  `output_retention_bytes`. Outputs of failing or unverified checks are kept.
- **Kept:** `run.json`, `manifest.json`, `policy.json`, `harness-wheel/`, `stages/`,
  `evidence.json`, `decision.json`, `report.md`, the logs, `observations.jsonl`,
  `correctness.jsonl`, `clifford.jsonl`, `cost/`, `changed-tests.json`, every `job.json` and
  `builds/<revision>.snapshot.json`.

Format `qtb-clean/1`:

| Field | Content |
| --- | --- |
| `status` | `cleaning` while deleting, then `complete` |
| `started_at`, `finished_at` | When the cleanup started and ended |
| `stage_state_hashes` | The stage-state hashes checked against `decision.json` |
| `planned` | Every path to delete. A directory: `kind: directory`, `files`, `bytes` and `listing_sha256` (a digest of its file list and sizes). A file: `kind: file`, `bytes`, `output_hash`, `observation_id`, `compressed_sha256`, `compressed_bytes`, and for a C6 prefix also `oracle`, `stage` and `job_file` |
| `freed_bytes` | The total size of the planned items |

`clean.json` is written with `status: cleaning` before anything is deleted. A killed `clean`
leaves that status; every stage then exits 41 until `clean` is run again, and the rerun
finishes the recorded list. After `clean`, `decide` still works and every other stage exits 41
with "Session cleaned". To remove a session entirely, delete its directory.

## The store

The store holds only baseline data, shared by every session that names it. `compile` takes it
from `--store` or `QTB_STORE` and records it in `run.json:store`.

```text
STORE/
  builds/<key>/          READY, build.json, env/, source/, cargo/, wheels/, build.log
  builds/<key>.lock      held while that key is being built
  wheels/<identity>/     baseline Qiskit wheels
  quality/<key>/         baseline quality observations with their output and job files;
                         invalidated.json after a failed determinism audit
  correctness/<key>/     rows.jsonl, evidence.json, provenance.json
  unit-tests/<key>/      python.json, rust.json, their logs, provenance.json
```

A build entry without `READY` is an interrupted build and is never read. A correctness or
unit-test entry is published in one rename; its `provenance.json` records the key, the session
that computed it and `first_computed`. Nothing about the evolved tree, and no cost sample, is
written to the store. Keys, sizes and maintenance are described in
[environments.md](environments.md#the-baseline-store).

## Observations (`observations.jsonl`)

One JSON object per compile of a quality case (schema `observation.json`, format
`qtb-observation/1`):

| Field | Content |
| --- | --- |
| `id` | Hash of case, case definition, build, revision, seed and block |
| `case_id`, `case_hash`, `revision`, `build_id`, `seed`, `seed_block` | Which compile this is |
| `D2`, `N2` | Metrics computed by the harness (absent if the compile failed or the circuit is dynamic) |
| `checks` | C0 always; C6 and C1-lite where applicable. Each has `oracle`, `status`, and details |
| `output`, `output_hash`, `layout` | Path and hash of the canonical output, and its layout arrays |
| `fingerprint` | Pass names per stage and readable search budgets (`unknown` when unreadable) |
| `worker` | The worker's raw result, including `compile_ns` and `status`/`error` |
| `cached`, `cached_from` | `true` and the store key when the observation was replayed from the store |

## Canonical circuit files (`*.ops.jsonl.gz`)

Inputs, outputs and references use one format (`qtb-circuit/1`): gzip-compressed JSON Lines, a
header line, then one operation per line in circuit order:

```text
{"format":"qtb-circuit/1","num_qubits":193,"num_clbits":0,"qregs":[["q",193]],"cregs":[],"global_phase":"0x0.0p+0","parameters":[]}
["rz",[17],[],["0x1.921fb54442d18p+0"],null]
["cz",[17,18],[],[],null]
```

Each operation is `[name, qubits, clbits, parameters, payload]`. Floats are C99 hex strings
(`float.hex()`), so values round-trip exactly. Non-standard operations (matrix gates, Pauli
evolution, annotated operations, control flow) carry a typed `payload`. The hash of a circuit
is the SHA-256 of its canonical uncompressed bytes, so gzip timestamps do not matter.

A layout is `{input_num_qubits, output_num_qubits, initial_index_layout, final_index_layout,
routing_permutation}`, or `null` when Qiskit attached none (identity).
