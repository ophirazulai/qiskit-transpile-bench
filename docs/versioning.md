# Format, profile, and compatibility rules

Canonical circuit/target artifacts, worker/verifier protocols, manifests, policies,
observations, constraint records, and decisions carry explicit version tags. Readers
reject unknown versions. Circuit hashes cover canonical uncompressed JSONL, including
typed payloads, parameter trees, register order, and phase. Target hashes preserve
instruction insertion order. Gzip timestamps do not affect canonical identity.

A fixture, semantic reference, role, seed block, canary constant, weight, constraint,
or threshold change requires a new profile version and fresh qualification.
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

Comparison writes its decision and report from the measured quality records and cost
bundles. An interrupted comparison can resume with its archived manifest, policy, and
matching coordinator version. Format migrations must write a new artifact, retain the
original, record both hashes and the migration code, and repeat the known-outcome tests.
There is no automatic migration that silently reinterprets an old decision.
