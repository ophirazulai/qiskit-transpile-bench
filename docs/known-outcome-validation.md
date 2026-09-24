# Known-outcome validation status

The local suite tests C7 mutations through the coordinator's constraint records:
dropping a Clifford `cz` must yield a C7 mismatch and `CONSTRAINT_VIOLATION`;
adding a non-Clifford `rz(0.001)` must yield C7 unverified and `INCONCLUSIVE`.
It also tests archived-observation replay with a scaled guard regression, cost-panel
restart after an incomplete bundle, Qiskit self-grading overrides, and identical-build
cache bypass. These tests use small circuits or synthetic observations; they do not
qualify a frozen profile.

The plan's section 11 A/A end-to-end check still requires a controlled runner and two
independent builds of the same source snapshot. Run `compare` with the same Qiskit source
as both `--baseline` and `--evolved` and a fresh results root. Before qualification,
inspect the saved `build.json` files to confirm independent baseline and evolved wheel
builds, then compare every B0 observation by case and seed: output hashes, `D2`, and
`N2` must match exactly; every paired delta must be zero. Check that evolved quality
observations were compiled rather than served from baseline cache, the determinism
audit passed, and the final decision is `NO_IMPROVEMENT`. Cost arms must have distinct
arm IDs even when their build identities match. Record the run directory and the
controlled-runner configuration with the qualification evidence.

Other section 11 workload controls, including the multiplier's C1-lite
contract and a real source-folder end-to-end comparison, also remain controlled-runner
qualification work.
