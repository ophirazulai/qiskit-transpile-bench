# Format, profile, and compatibility rules

[Documentation index](README.md)

Canonical circuit/target artifacts, worker/verifier protocols, manifests, policies,
observations, constraint records, and decisions carry explicit version tags. Readers
reject unknown versions. Circuit hashes cover canonical uncompressed JSONL, including
typed payloads, parameter trees, register order, and phase. Target hashes preserve
instruction insertion order. Gzip timestamps do not affect canonical identity.

Session files are tagged as well: `run.json` is `qtb-run/3` (it records the store and, for
monitored sessions, the required cost evidence, and is written only by `compile`), each
`stages/<stage>/state.json` is `qtb-stage/2`, and `clean.json` is `qtb-clean/1`. Their fields
are listed in [output-format.md](output-format.md). A change to these fields requires a new
tag.

- `qtb-stage/2` adds the `noisy` status, the `invocation` field and `contamination`. A
  `qtb-stage/1` reader does not know `noisy` and could take such a stage for something it
  can finish or clean, so the tag changed. Current readers accept both and refuse any other.
- `qtb-run/3` adds `cost_evidence`. `qtb-run/2` sessions are still read and decided; their
  cost bundles are machine-mode or unlabelled, and the report labels them as unmonitored.
  They are never promoted to monitored evidence, and an active session is never migrated.

## The monitor contract

Monitored cost evidence follows contract `qtb-lsf-monitor/1`, frozen in
`lsf/cost_monitor.py`: the checks (A, foreign CPU on the worker core; B, involuntary
preemption of the worker), their thresholds (5 % of physical-core time with a 0.03 s floor;
4 switches per second with a floor of 2), the 2-second window, the 2-second idle probe, the
nine-core allocation and the CPU layout (monitor on the first core, worker on the second).
The bundle evidence is tagged `qtb-lsf-monitor-evidence/1`.
Its worker records include acknowledged measured intervals for every entry. Windows split
at their boundaries, excluding setup and warmup from measured-window denominators. Evidence
without those intervals is inadmissible.

- A session records the contract, the thresholds, the layout, the approved hardware tier and
  the identity of the measurement code (`lsf/context.py`, `lsf/cost_monitor.py`,
  `lsf/cost_evidence.py`) in `run.json:cost_evidence` when it is created. Removing fields
  from a bundle cannot opt out: a bundle without admissible evidence is `unresolved`.
- Any change to a threshold, the window, the checks, the layout or the allocation is a new
  contract version, even when it comes from calibration. The profile's cost thresholds
  (`policy.json:cost_thresholds`) are separate and keep their own rule: changing them is a
  new profile version.
- The whole `lsf` package (not its tests) is part of the harness identity and the archived
  harness wheel. Changing it, the monitor or the allocation adapter included, makes later
  stages of an existing session refuse to run; start a new session. Queue, memory and run
  limits are site settings, not part of the harness.
- `decide` accepts only an evidence extension that implements the session's contract. A
  missing or incompatible extension refuses the evidence rather than accepting it unchecked.

The LSF orchestration records carry their own tags: `qtb-lsf-launch/1`, `qtb-lsf-ledger/1`,
`qtb-lsf-outcome/1`, `qtb-lsf-report/1` and `qtb-lsf-context/1`. Readers reject unknown
ledger formats.

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
only complete bundles from the same session can be resumed, and in a monitored session only
bundles whose monitoring evidence is admissible.

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
