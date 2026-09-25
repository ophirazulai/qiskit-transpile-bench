# Format, profile, and compatibility rules

Canonical circuit/target artifacts, worker/verifier protocols, manifests, policies,
observations, constraint records, and decisions carry explicit version tags. Readers
reject unknown versions. Circuit hashes cover canonical uncompressed JSONL, including
typed payloads, parameter trees, register order, and phase. Target hashes preserve
instruction insertion order. Gzip timestamps do not affect canonical identity.

Session files are tagged as well: `run.json` is `qtb-run/2` (it records the store and is
written only by `compile`), each `stages/<stage>/state.json` is `qtb-stage/1`, and
`clean.json` is `qtb-clean/1`. Their fields are listed in [output-format.md](output-format.md).
A change to these fields requires a new tag.

A fixture, semantic reference, role, seed block, canary constant, weight, constraint,
or threshold change requires a new profile version.
Do not edit a frozen profile to accept a candidate. Recuration is an explicit maintenance
operation using `tools/curate/curate.py`; comparisons consume only frozen artifacts.
The original RevLib/HWB claims do not transfer to their declared generated replacements.

Both profiles are at version 5. Version 5 removed qualification: the `harness/qualification`
required ID and the policy's `qualification` object are gone, and the manifests no longer
carry a `status`. It also removed `CA1/upstream` from the confirm policy's `required_ids`;
`decide` now requires `*1/upstream` on either profile once the optional `unit-tests` stage has
started. `required_ids` is a constraint, so the change needed the version bump. The stored
baseline quality key covers the case definition and the quality protocol, not the policy
hash, so the bump does not by itself invalidate stored baseline quality.

Adding or reweighting cases invalidates aggregate decisions while preserving stored baseline
quality entries for unchanged case definitions. Input/reference/target/options changes
invalidate the affected case. Source content, Python, dependencies, Rust toolchain and
build flags enter build identity; a stored baseline build is also keyed by the harness wheel.
Only baseline wheels are kept, in the store; the evolved tree is compiled once per session.
Worker protocol, coordinator/harness code, serial environment, machine, and measurement
policy scope quality evidence. Fresh cost sessions always collect independent arms;
only complete bundles from the same session can be resumed.

The CI reference is Qiskit 2.5.2 on Python 3.11–3.13. The semantic verifier is independently
pinned to 2.5.2. Source workers use explicit adapters and reject unsupported operations or
state representations. A successful build alone does not establish compatibility with
another Qiskit revision: input round trips and all applicable correctness checks remain
required, and an A/A session with the known-outcome tests should validate a new runner. No
broad future-version guarantee is made.

`decide` writes the decision and report from the committed stage evidence, the quality
records and the cost bundles. A stage that was interrupted resumes with the session's
archived manifest and policy and the harness that compiled the session; a stage refuses to
run (exit 41) when the coordinator, implementation, manifest or policy changed since
`compile`. Format migrations must write a new artifact, retain the original, record both
hashes and the migration code, and repeat the known-outcome tests. There is no automatic
migration that silently reinterprets an old decision.

There is no `evaluate` command. `decide` re-decides a session from its archived manifest and
policy, checked against the hashes in `run.json`, and from the committed evidence and cost
bundles. It accepts a newer harness, and the report then notes that a different harness
version decided than the one that compiled. It works after `clean` too. It replaces the
session's `evidence.json`, `decision.json`, `report.md` and `progress.log`; copy them first to
keep an earlier verdict.
