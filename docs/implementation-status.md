# Implementation and qualification

The implementation follows `design/transpilation-benchmark-impl-plan.md`. Both profiles
are version 1 and deliberately marked unqualified. A successful smoke test is not benchmark
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
  separately calibrated doubled-count reruns, calibration expiry, and archived bundles.
- Snapshot/build/provenance isolation, per-seed worker records and timeouts, durable run
  evidence, content-addressed quality cache, determinism audits, reporting, review and repro.
- Trusted dense, statevector, terminal-measurement, dynamic-branching, Clifford, and schedule
  oracles; a frozen 480-configuration C1–C5 fixture suite; worker API contract observations.
- Curated iterations and confirm workloads, frozen semantic references for Trotter inputs,
  Clifford variants, explicit license/provenance records, and reproducible curation tools.
- CI and an opt-in controlled-runner workflow with archived evidence.

## Qualification work that requires a runner and review

1. Run both baseline builds, the complete correctness suite, B0/KB1/KB2 quality collection,
   all cost calibrations, and the known-outcome mutations on the intended runner. Review
   every proposed deterministic/canary role against the measured baseline; draft probe facts
   are not sufficient. The replacement reversible circuits require fresh measurements.
2. Derive output-pinned upstream exclusions against a seed-reshuffled baseline, then review
   and freeze the node IDs in the profile's `exclusions.json`. Until it is reviewed, the
   upstream requirement stays unresolved. New binding Python failures and executed Rust
   test failures must be investigated independently of score changes.
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
  forms. It does not silently decompose a high-level input to make a revision compatible.
  Target angle-bound export is currently unsupported; the shipped targets have no bounds.
- The automatically generated behavioral suite covers the implemented oracle paths; a
  complete upstream exclusion derivation, coherent-control phase checks, and every negative
  API configuration from the design still need qualification expansion.
- Quality execution is serial in the coordinator, with seeds batched for compilation but
  routing prefixes launched separately. It preserves the serial reference contract, but
  does not yet meet the plan's concurrent-runner wall-time estimates.
- Full C1-lite invocation currently selects the small-band scored cases. Extending eligibility
  to every case satisfying the union-width rule is required before confirm qualification.
- Full output retention is conservative: outputs are kept for inspection. The 8 MB pruning
  policy is not yet applied. Build reuse across separate comparisons is not implemented;
  quality evidence remains content-addressed and reusable when build identities match.
- The cost estimators and sign-flip calibrator have independent tests. Combined family-wise
  quality/cost rejection calibration and all 300-seed role-freeze checks require completion
  before freezing a profile. The qualification gate prevents acceptance without that work.

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
