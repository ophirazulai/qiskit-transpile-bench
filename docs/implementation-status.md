# Implementation and qualification

The implementation originated from `design/transpilation-benchmark-impl-plan.md`; the
README describes the current CLI. Both profiles
are version 2 and deliberately marked unqualified. A successful smoke test is not benchmark
qualification. Numerical examples and synthetic acceptance-path tests are harness tests,
not measurements of an evolved Qiskit revision.

## Implemented components

- Versioned manifest, policy, job/result, observation, constraint, and decision schemas.
- Canonical hex-number circuit/target IO, deterministic gzip and content hashes, typed
  matrices, Pauli evolution with synthesis settings, annotated operations, MCMT and selected
  arithmetic-library operations, symbolic expressions, delay units, and bounded control flow.
- Streaming structural metrics and legality, ordered connectivity, full layout validation,
  exact routing replay including permutations removed by init.
- Paired log estimators, quality caps, exact and zero-baseline guards, family/level summaries,
  confirm breadth, leave-iterations-out reporting, family cluster bootstrap, and sign-flip
  calibration. The verdict procedure preserves candidate-failure precedence.
- Estimator-specific A/A cost calibration, fresh three-arm sessions, independent arm IDs,
  separately calibrated doubled-count reruns, calibration expiry, archived bundles, and
  combined quality/cost false-rejection calibration.
- Snapshot/build/provenance isolation, per-seed worker records and timeouts, durable run
  evidence, wheel and quality caches, determinism audits, and reports generated from
  quality and cost observations.
- Trusted dense, statevector, terminal-measurement, dynamic-branching, Clifford, and schedule
  oracles; a frozen 480-configuration C1–C5 fixture suite; coherent-control phase checks;
  worker API contracts and negative configuration checks.
- Curated iterations and confirm workloads, frozen semantic references for Trotter inputs,
  Clifford variants, explicit license/provenance records, and reproducible curation tools.
- All-300-seed baseline role audits, C1-lite eligibility based on the reference/output union,
  and binding baseline-owned test execution.
- One-pass structural metrics/hash/legality, successful-output pruning above 8 MB,
  runner-wide exclusion of quality jobs during cost measurements, and batched routing prefixes.
- CI and an opt-in controlled-runner workflow with archived evidence.

## Implementation validation (2026-09-24)

- All 86 automated tests pass; repository-wide Ruff and whitespace checks pass.
- A native smoke run built two isolated release wheels from local Qiskit revision
  `c062c2dfd240b23157fbfcf6184e8998a9c8b343` (2.6.0.dev0) and completed all 24 quality
  observations: 12 iteration cases in each build. Smoke does not produce a benchmark verdict.
- All 120 level-2 behavioral fixture configurations passed against the pinned 2.5.2
  worker/verifier setup. Other levels are exercised by the full qualification suite.
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
they do not qualify a controlled runner or demonstrate a candidate improvement.

## Qualification work that requires a runner and review

1. Run both baseline builds, the complete correctness suite, B0/KB1/KB2 quality collection,
   all cost calibrations, and the known-outcome mutations on the intended runner. Review
   every proposed deterministic/canary role against the measured baseline; draft probe facts
   are not sufficient. The replacement reversible circuits require fresh measurements.
   Before qualification, use `tools/freeze_timeouts.py measure` against the baseline build
   and `freeze` to draft version-bumped manifests. The tool requires measured compiles for
   every case and seed, including `multiplier_h18_n20`; the shipped version-2 profiles
   still carry provisional 120-second timeouts.
2. Investigate binding upstream Python and Rust test failures independently of score changes.
   The comparison runs these tests directly, without an exclusion list.
3. Qualify the corrected C1-lite measurement oracle on the output/reference union, including
   the larger eligible `ripple_adder_10` case, and inspect the actual coverage and runtime.
4. Inspect a complete real comparison and create a qualification record under
   `RESULTS_ROOT/qualifications/HASH.json`, where HASH is the canonical SHA-256 of the run's
   `hashes` object. It must contain matching `hashes`, matching `machine`, a named `reviewer`,
   a `controlled_run` evidence reference, and `known_outcomes_passed: true`. This is a
   maintainer attestation, not a command-line switch to skip measurements. Every other
   required record still has to pass.

## Current limitations to resolve before qualification

- The full controlled-runner qualification and manual evidence inspection have not been
  performed by this implementation task. Neither profile should be presented as qualified.
- The adapter intentionally refuses unknown operations and unsupported expression/control-flow
  forms. It does not silently decompose high-level inputs to make a revision compatible.
  Angle-bound targets use an explicit, checked Qiskit state adapter because 2.5.2 has no
  public getter for the numeric bounds.
- Quality orchestration is serial, with quality and routing prefixes batched by seed.
  Runtime and memory budgets still need measurement on the intended controlled runner.
- Automatic pruning keeps failing and inconclusive runs intact for investigation. Successful
  large outputs are pruned with a hash/observation retention record. Pinned dependency
  downloads are required when they are absent from the local package cache.
- The candidate's own Python tests are report-only, while the baseline's tests bind the
  comparison.
- CI currently verifies the adapter on Qiskit 2.5.2. The trusted verifier stays pinned to that
  version. Additional source revisions must complete round trips and the correctness suite
  before they can participate in a qualified comparison.

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
