# Implementation and runner validation

[Documentation index](README.md)

The implementation originated from `design/transpilation-benchmark-impl-plan.md`; the
README describes the current CLI. Both profiles are version 5. There is no qualification
step: a session can end `PASS` when every required record passes. Validating the harness on a
new runner is a practice, described below, not a required record. A successful `compile` is
not a validation. Numerical examples and synthetic acceptance-path tests are harness tests,
not measurements of an evolved Qiskit revision.

## Implemented components

- Versioned manifest, policy, job/result, observation, constraint, and decision schemas.
- Canonical hex-number circuit/target IO, deterministic gzip and content hashes, typed
  matrices, Pauli evolution with synthesis settings, annotated operations, MCMT and selected
  arithmetic-library operations, symbolic expressions, delay units, and bounded control flow.
- Streaming structural metrics and legality, ordered connectivity, full layout validation,
  exact routing replay including permutations removed by init.
- Paired log estimators, quality caps, exact and zero-baseline guards, family/level summaries,
  confirm breadth, leave-iterations-out reporting, and family cluster bootstrap. The verdict
  procedure preserves candidate-failure precedence.
- Fixed policy cost thresholds, fresh two-arm sessions, independent arm IDs, screened and
  doubled-count rerun regimes, and archived bundles replayable from the archived policy.
- Snapshot/build/provenance isolation, per-seed worker records and timeouts, durable
  per-stage evidence, determinism audits, and reports generated from quality and cost
  observations.
- Separately runnable stages against one session directory: `compile`, `quality`,
  `correctness`, optional `unit-tests`, `cost`, `decide` and `clean`. A quality gate runs
  correctness, unit tests and cost only after an improvement or on an A/A run. Each stage
  has its own state, evidence, lock and progress log, can run on a different host, and
  resumes when run again after a failure or interruption. `decide` can be repeated at any
  time, also after `clean`.
- A baseline store shared by sessions: baseline builds, wheels, quality observations,
  correctness results and unit-test results, each under a content key. Stored builds are
  never modified, and a failed determinism audit invalidates the stored baseline quality.
- Trusted dense, statevector, terminal-measurement, dynamic-branching, Clifford, and schedule
  oracles; a frozen 480-configuration C1–C5 fixture suite; coherent-control phase checks;
  worker API contracts and negative configuration checks.
- Curated iterations and confirm workloads, frozen semantic references for Trotter inputs,
  Clifford variants, explicit license/provenance records, and reproducible curation tools.
- C1-lite eligibility based on the reference/output union, and binding baseline-owned test
  execution.
- One-pass structural metrics/hash/legality, `clean` pruning of verified outputs above 8 MB,
  runner-wide exclusion of quality jobs during cost measurements, and batched routing prefixes.
- CI and an opt-in controlled-runner workflow with archived evidence.
- LSF orchestration under `lsf/`: a launcher, a manager job that runs every stage as its own
  job with fixed allocations, a durable ledger with restart-safe cost retries (one initial job
  and at most 20 retries), deadline and cancellation handling, `status`/`stop`/`reap`, an
  orchestration report and structured logs. A cost monitor on nine exclusive cores with an
  idle probe and checks A and B per window, and an evidence validator that `decide` applies
  to every monitored bundle, also after `clean`.

## Implementation validation (2026-09-24)

- All 86 automated tests pass; repository-wide Ruff and whitespace checks pass.
- A native run of the former `smoke` command (removed; `compile` now does the builds and the
  round-trip) built two isolated release wheels from local Qiskit revision
  `c062c2dfd240b23157fbfcf6184e8998a9c8b343` (2.6.0.dev0) and completed all 24 quality
  observations: 12 iteration cases in each build. It did not produce a benchmark verdict.
- All 120 level-2 behavioral fixture configurations passed against the pinned 2.5.2
  worker/verifier setup. Other levels are exercised by the full correctness suite.
- With the native 2.6.0.dev0 worker, all three 100-qubit Clifford prefixes verified. The full
  Heisenberg variant also verified; the full QFT and QAOA variants remained unverified
  because their compiled outputs contained non-Clifford rotations.
- The eligible `ripple_adder_10` C1-lite check verified on a 23-qubit union. End-to-end timing,
  reused-pass-manager timing, preset construction, and memory worker modes completed.
- The zero-initialized `multiplier_h18_n16` positive control verified at level 0, seed 0
  on Mumbai using a 16-qubit union.
- The built wheel includes all 271 confirm cases, and its coordinator imports without Qiskit.

Local evidence is under `results/validation/summary.json`, `results/smoke-validation/`,
and `results/compatibility-validation/`. These checks establish implementation behavior;
they do not validate a runner or demonstrate a candidate improvement.

## LSF orchestration validation (2026-09-26)

- All 323 automated tests pass on macOS (Python 3.11), including 89 under `lsf/tests/`:
  allocation translation, the monitor on synthetic counters and topology (SMT, missing
  metrics, mismatched masks, brief contamination, each check alone, timeout restarts, the
  final window), evidence admission (forged and missing records, missing and incompatible
  extensions, replay after cleanup), and the manager against a fake scheduler running every
  job in-process through the real entry point (fixed allocations, gate-closed jobs, exactly
  20 retries, a manager killed mid-submission, a killed last attempt, lost submission
  answers, rejected submissions, queue deadlines, failed stages, duplicate managers,
  signals, requeue and `reap`, log levels and correlation fields).
- In a Linux container (5 vCPUs of a laptop VM), the real `LinuxProbe` path ran through
  `run_worker`: the worker was placed on its core before `exec`, windows were contiguous and
  the final one was taken between exit and reap. The worker CPU summed over windows (4.49 s)
  matched the reaped worker's usage (4.494 s), with 0.01–0.03 s of foreign time per clean
  window. A CPU burner pinned to the worker core failed checks A and B in the first window
  (1.13 s of foreign CPU in 2.04 s), and the aborted worker was reaped. The 105 LSF and
  worker-hook tests also pass there.
- On that VM, an unloaded vCPU showed about 6 involuntary switches per second, above check
  B's 4 per second: a shared laptop vCPU is not a quiet exclusive core, and it shows why the
  thresholds must be calibrated on the cluster tier.

Nothing has been submitted to LSF: the orchestration is validated against a fake scheduler
only, and the monitor's thresholds are uncalibrated.

## Monitored cost on LSF

Before relying on cost verdicts from the cluster, on the selected hardware tier and CPU
layout:

1. Run quiet A/A sessions and record every window's foreign fraction and involuntary rate, to
   set the thresholds, the window length and the false-rejection rate. Include `DEBUG`
   logging in the overhead measurement.
2. Inject controlled contention only inside allocations you own: load on the worker core, on
   its SMT sibling, and on other cores of the allocation stressing shared cache and memory
   bandwidth. Checks A and B must catch the first two; the third is outside what they detect.
3. Run end-to-end A/A and known-slowdown sessions through `lsf/submit.py`: clean runs keep
   their expected outcomes, contaminated runs are resubmitted, a genuine regression survives
   the policy rerun, exhaustion never produces a `PASS`, and no job remains after the manager
   exits (`lsf.control status`).
4. Settle the site values: the verified hardware selector, the queues, memory and run limits,
   and the manager's deadline.

A calibrated threshold or window is a new monitor contract version ([versioning](versioning.md#the-monitor-contract)).

## Validation on a new runner

Nothing in the harness enforces these steps or records that they were done. They are how to
find out whether a `PASS` on a given runner can be trusted.

1. Run one A/A session (the same source as `--baseline` and `--evolved`) through every stage
   on the intended runner: both builds, B0 quality collection, the complete correctness
   suite, and the cost panels, which check the fixed cost thresholds against the runner's own
   drift. Run the known-outcome mutations
   ([known-outcome controls](validation.md#known-outcome-controls)). Review every proposed
   deterministic/canary role against the measured baseline; draft probe facts are not
   sufficient. The replacement reversible circuits require fresh measurements.
   Use `tools/freeze_timeouts.py measure` against the baseline build and `freeze` to draft
   version-bumped manifests. The tool requires measured compiles for every case and seed,
   including `multiplier_h18_n20`; the shipped version-5 profiles still carry provisional
   120-second timeouts.
2. Investigate binding upstream Python and Rust test failures independently of score changes.
   The optional `unit-tests` stage runs these tests directly, without an exclusion list, and
   once started they bind the verdict.
3. Validate the corrected C1-lite measurement oracle on the output/reference union, including
   the larger eligible `ripple_adder_10` case, and inspect the actual coverage and runtime.
4. Inspect a complete real session (`report.md`, `decision.json` and the stage states under
   `stages/`) before relying on its verdict.

## Current limitations

- The runner validation above and manual evidence inspection have not been performed by this
  implementation task. A `PASS` is reachable, but no runner has been checked against known
  outcomes.
- The LSF orchestration has not run on a cluster, and the monitoring thresholds are IOCR's
  uncalibrated starting points ([monitored cost on LSF](#monitored-cost-on-lsf)).
- The adapter intentionally refuses unknown operations and unsupported expression/control-flow
  forms. It does not silently decompose high-level inputs to make a revision compatible.
  Angle-bound targets use an explicit, checked Qiskit state adapter because 2.5.2 has no
  public getter for the numeric bounds.
- Quality orchestration runs up to 9 seed batches at once (each batch is one quality job and
  its two routing prefixes). Input roundtrip and the determinism audit also run concurrently.
  The C0/C6 checks of each batch run in the coordinator process and are bound by the GIL.
  Runtime and memory budgets still need measurement on the intended controlled runner.
- Sessions are not pruned automatically, apart from the Rust `target/` directories of each
  build and test run. `clean` removes a decided session's builds,
  verifier, caches and test copies, and replaces verified outputs above 8 MB with a hash
  record in `clean.json`; failing and unverified outputs are kept. The baseline store is
  maintained by hand. Pinned dependency downloads are required when they are absent from the
  local package cache.
- The candidate's own Python tests are report-only, while the baseline's tests bind the
  verdict once `unit-tests` has started.
- CI currently verifies the adapter on Qiskit 2.5.2. The trusted verifier stays pinned to that
  version. Additional source revisions must complete round trips and the correctness suite
  before their results can be trusted.

## Fixture substitutions

The plan explicitly permits generated reversible circuits when redistribution provenance
cannot be established. This version uses the following distinct constructors from Qiskit's
baseline `test/benchmarks/utils.py`, without assigning them the original circuits' identities:

| Original | Replacement |
| --- | --- |
| `revlib_4gt10_v1_81` | `mcx_kg24_n5` |
| `revlib_mod8_10_178` | `mcx_kg24_n6` |
| `revlib_cnt3_5_179` | `adder_modular_v17_n8` |
| `revlib_cnt3_5_180` | `adder_modular_v17_n16` |
| `revlib_4mod5_v0_19` | `adder_modular_v17_n6` |
| `hwb12` | `multiplier_h18_n20` |

The group counts, level structure, and printed weight examples are preserved. The original
HWB runtime estimates and original RevLib quality facts do not apply to these replacements.

## Known-outcome controls

The local suite tests C7 mutations through the coordinator's constraint records:
dropping a Clifford `cz` must yield a C7 mismatch and `CONSTRAINT_VIOLATION`;
adding a non-Clifford `rz(0.001)` must yield C7 unverified and `INCONCLUSIVE`.
It also tests archived-observation replay with a scaled guard regression, cost-panel
restart after an incomplete bundle, Qiskit self-grading overrides, and the bypass of
stored baseline quality rows for identical builds. These tests use small circuits or
synthetic observations; they do not validate a runner or a frozen profile.

The plan's section 11 A/A end-to-end check still requires a controlled runner and two
independent builds of the same source snapshot. Run the stages with the same Qiskit source
as both `--baseline` and `--evolved`, in a new session directory. With identical build IDs
the quality gate is `aa`, so `correctness` and `cost` run as a check of the harness, and no
stored baseline quality, correctness or unit-test results are reused. Inspect the
`build.json` files (the baseline's in its store entry, the evolved one's in
`builds/evolved-build/`) to confirm independent baseline and evolved wheel builds, then
compare every B0 observation by case and seed: output hashes, `D2`, and `N2` must match
exactly; every paired delta must be zero. Check that no observation carries `cached_from`,
the determinism audit passed, and the final decision is `NO_IMPROVEMENT`. Cost arms must
have distinct arm IDs even when their build identities match. Keep the session directory
and the controlled-runner configuration as the validation evidence.

Other section 11 workload controls, including the multiplier's C1-lite
contract and a real source-folder end-to-end comparison, also remain validation work for a
controlled runner.
