# Known-outcome validation status

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
