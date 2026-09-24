# Format, profile, and compatibility rules

Canonical circuit/target artifacts, worker/verifier protocols, manifests, policies,
observations, constraint records, and decisions carry explicit version tags. Readers
reject unknown versions. Circuit hashes cover canonical uncompressed JSONL, including
typed payloads, parameter trees, register order, and phase. Target hashes preserve
instruction insertion order. Gzip timestamps do not affect canonical identity.

A fixture, semantic reference, role, seed block, canary constant, weight, constraint,
exclusion list, or threshold change requires a new profile version and fresh qualification.
Do not edit a frozen profile to accept a candidate. Recuration is an explicit maintenance
operation using `tools/curate/curate.py`; comparisons consume only frozen artifacts.
The original RevLib/HWB claims do not transfer to their declared generated replacements.

Adding or reweighting cases invalidates aggregate decisions while preserving compilation
cache entries for unchanged case definitions. Input/reference/target/options changes
invalidate the affected case. Source content, Python, dependencies, Rust toolchain and
build flags enter wheel identity; baseline/control/evolved wheel cache slots are separate.
Worker protocol, coordinator/harness code, serial environment, machine, and measurement
policy scope quality evidence. Fresh cost sessions always collect independent arms;
only complete bundles from the same comparison can be resumed.

The CI reference is Qiskit 2.5.2 on Python 3.11–3.13. The semantic verifier is independently
pinned to 2.5.2. Source workers use explicit adapters and reject unsupported operations or
state representations. A successful build alone does not establish compatibility with
another Qiskit revision: input round trips, all applicable correctness checks, and the
controlled-runner qualification remain required. No broad future-version guarantee is made.

Historical evaluation checks archived manifest/policy and coordinator identities and
recomputes from saved quality records and original cost bundles/calibration. It preserves
measurement timestamps and human review. Use the archived harness wheel to replay an older
coordinator. Format migrations must write a new artifact, retain the original, record both
hashes and the migration code, and repeat the known-outcome tests. There is no automatic
migration that silently reinterprets an old decision.

`derive-exclusions RUN` executes baseline-owned tests twice in the baseline environment,
with the second run offsetting explicit SABRE seeds by the frozen constant 1,000,003.
It archives test node IDs, outcomes, failure text and the runner hash. Review the proposed
new failures for exact heuristic-output assertions, remove semantic failures, and freeze
the reviewed list with its `baseline_build` in the versioned profile. A proposal never
satisfies the upstream requirement by itself.
