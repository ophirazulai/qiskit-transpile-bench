# Output: the run directory, `decision.json` and `report.md`

Each `compare` or `smoke` invocation creates one run directory:

```text
results/runs/<YYYYMMDDTHHMMSS>-<8 hex>/
```

The timestamp is UTC. `--results-root DIR` changes `results/`. The command prints the run
directory when it finishes:

```text
PASS (iterations-profile): /…/results/runs/20260924T135815-5702c324
SMOKE OK: /…/results/runs/20260924T135815-5702c324
```

## What to open first

| You want | Open |
| --- | --- |
| The verdict and why, in words | `report.md` |
| The verdict for scripts or CI | the exit code, or `decision.json` → `status` |
| Whether `smoke` passed | `smoke.json` → `success` |
| Why a build failed | `builds/<revision>-build/build.log` |
| Why one compile failed | `jobs/<id>/worker.log` and `jobs/<id>/out/results.jsonl` |

## Exit codes

| Code | Status | Meaning |
| ---: | --- | --- |
| 0 | `PASS` | Improvement shown and every required constraint satisfied (smoke: success) |
| 10 | `NO_IMPROVEMENT` | Valid, complete panel, but the improvement rule was not met; nothing violated |
| 20 | `CONSTRAINT_VIOLATION` | The candidate failed a correctness check or an established quality/cost guard |
| 30 | `INCONCLUSIVE` | Something required is missing, unresolved or unqualified, or the baseline itself failed |
| 40 | `ERROR` | Harness, build or input failure (smoke: failure) |
| 64 | — | Command-line usage error |

## Directory contents

| Path | Written by | Content |
| --- | --- | --- |
| `run.json` | Coordinator | Run ID, profile, hashes (manifest, policy, coordinator, harness wheel, implementation), machine identity, source paths, builds, changed paths, change scope per level, calibration summary, status |
| `manifest.json`, `policy.json` | Coordinator | Archived copies of the profile used, so a run can be re-evaluated even if the profile changes |
| `evidence.json` | Coordinator | Every constraint record gathered so far: round-trip, preflight, correctness, calibration, audit, cost |
| `observations.jsonl` | Coordinator | One line per quality compile (see below) |
| `correctness.jsonl` | `compare` | Every C0–C5 check from the correctness suite |
| `clifford.jsonl` | `compare` | C7 results for full and prefix pipelines |
| `role-freeze.jsonl` | `compare` (first calibration) | 300-seed baseline audit of deterministic, zero-baseline and canary roles |
| `changed-tests.json` | `compare` | Test files and Rust sources that the evolved tree changed |
| `upstream-baseline/`, `upstream-evolved/` | `compare` | Upstream pytest records (`tests.jsonl`), logs and `rust.log` |
| `upstream-evolved-own.json` | `compare` | Report-only run of the candidate's own Python tests |
| `cost/<panel>/screen.json`, `normal.json`, `rerun.json` | `compare` | Raw three-arm timing or memory bundles, one per regime reached; sessions in subdirectories. A timing session holds one `timing_batch` job per round and arm |
| `builds/` | Coordinator | Snapshots, build directories, `build.json`, `build.log` ([environments.md](environments.md)) |
| `verifier/` | Coordinator | Verifier environment |
| `harness-wheel/`, `harness-build.log` | Coordinator | The harness wheel installed everywhere |
| `jobs/<uuid>/` | Worker | `job.json`, `worker.log`, `out/results.jsonl`, output circuits `out/output-<seed>.ops.jsonl.gz` |
| `oracle-jobs/<uuid>/` | Verifier | `job.json`, `result.json`, `verifier.log` |
| `decision.json` | Reporter | Machine-readable verdict (`compare` only) |
| `report.md` | Reporter | Human-readable verdict (`compare` only) |
| `smoke.json` | `smoke` | Smoke result; no decision is written |
| `retention.json` | Coordinator | Large successful outputs that were deleted, with their hashes |
| `reevaluations/<timestamp>/` | `evaluate --run` | A recomputed `decision.json` and `report.md`; the original is never changed |

Outside the run directory, the results root also holds shared caches and counters:
`build-cache/`, `quality-cache/`, `calibrations/`, `decision-counts.json` and `qualifications/`.
See [architecture.md](architecture.md#caches-and-shared-state-in-the-results-root).

## `smoke.json`

```json
{"format": "qtb-smoke/1", "success": true, "run_id": "20260924T135815-5702c324", "observations": 24}
```

On failure: `{"format": "qtb-smoke/1", "success": false, "error": "Build command exited 1; log: …/build.log"}`.
`success` requires one observation per quality case per revision, and every C0 check verified.

## `decision.json`

Schema: `src/qtb/config/schemas/decision.json` (format `qtb-decision/1`).

| Field | Content |
| --- | --- |
| `status` | `PASS`, `NO_IMPROVEMENT`, `CONSTRAINT_VIOLATION`, `INCONCLUSIVE` or `ERROR` |
| `improved_under_constraints` | `true` for `PASS`, `false` for `NO_IMPROVEMENT` and `CONSTRAINT_VIOLATION`, `null` otherwise |
| `profile`, `run_id`, `seed_block` | Which profile, which run; always block `B0` |
| `hashes` | Manifest, policy, coordinator, harness wheel and implementation hashes |
| `identities` | Build ID of each revision (`baseline`, `evolved`, `control`) |
| `decisions_before` | How many earlier decisions the results root holds for this manifest |
| `constraints` | Every constraint record: `id`, `kind`, `subject`, `result`, plus details such as `value`, `SE`, `threshold` and `detail` |
| `required_ids` | The record IDs that must all pass for `PASS` |
| `reasons` | Every record that did not pass: `code`, `result`, `detail` |
| `missing_records` | Required IDs with no record at all |
| `summaries` | Per-panel estimates: `ln_score`, `score`, `SE`, `ln_score_plus_2SE`, per-seed `deltas`, and per-case estimates with `worst_seed`; confirm also has `instance_bootstrap` and `leave_iterations_out` |
| `scope` | Changed stages, substituted components and unmapped paths, per optimization level |
| `calibration` | Noisy-guard count and false-rejection rates (quality, cost, combined) |
| `fingerprint_changes` | Cases whose pipeline fingerprint (pass list and search budgets, seed 0) differs between revisions |
| `coverage_gaps` | The profile's declared coverage gaps |
| `measurement_timestamp` | When the run was created |
| `reevaluation` | Only in `reevaluations/…`: evaluator identity, original decision hash, time |

A constraint record looks like this:

```json
{"id": "IA2/improvement", "kind": "improvement", "result": "passed", "subject": "evolved",
 "reference": "baseline", "value": -0.0213, "SE": 0.0054, "threshold": 0.0}
```

`value` is `ln(score)`. The rule that was applied is described in [metrics.md](metrics.md#4-decision-rules).

## `report.md`

The same information as `decision.json`, written for people. Sections, in order:

1. **Title:** `# <STATUS> — <profile>`, the number of earlier decisions for this manifest,
   and a reminder that the baseline is the reference and that confirm is a check, not a
   tuning loop.
2. **Calibration line** (when available): number of noisy guards and the calibrated
   family-wise false-rejection estimate (quality, cost, combined).
3. **Constraints needing attention:** every record that is `failed`, `unresolved` or
   `not_evaluated`, with its detail, and every required record that is missing. **Start here
   when the verdict is not `PASS`.**
4. **Quality estimates:** one row per panel: ratio (`score`; below 1 is better), paired SE in
   log units, and the upper bound `ln(score) + 2·SE`. The confirm profile adds the report-only
   instance bootstrap.
5. **Per-case changes:** each case and metric with its seed-aggregated ratio and worst seed.
6. **Compilation cost:** each cost panel's result and the candidate's and control arm's
   log time ratios against the baseline.
7. **Coverage and provenance:** the stage-coverage rule, the change scope as JSON, declared
   workload gaps, and the cases whose pipeline fingerprints changed.

An illustrative excerpt (numbers invented to show the layout):

```markdown
# INCONCLUSIVE — iterations-profile

Earlier decisions for this manifest: 3.

## Constraints needing attention

- **unresolved** `harness/qualification`: Requires known-outcome validation and inspected controlled-runner evidence
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
`INCONCLUSIVE` because the change touched a stage no verified check covers, and because the
configuration is not qualified.

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
| `cached` | `true` when the observation came from the quality cache |

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

## Re-evaluating a finished run

```bash
uv run qiskit-transpile-bench evaluate --run results/runs/<run>
```

This recomputes the verdict from the archived manifest, policy, observations, evidence and
cost bundles, without compiling anything. It checks the archived hashes and writes a new
`decision.json` and `report.md` under `reevaluations/<timestamp>/`. Use it after fixing an
evaluator or reporting bug.
