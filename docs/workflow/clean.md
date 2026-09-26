# 7. clean

[Documentation index](../README.md) · [← decide](decide.md)

Reclaim one session’s bulk while retaining its results. Cleanup is `decide`'s default
follow-up and is final for measurement stages.

## Run this step

```bash
uv run qiskit-transpile-bench clean --results-root "$S"
```

`S` is the session directory chosen at `compile`. You rarely need this command: `decide`
cleans the session after writing the verdict ([cleanup afterwards](decide.md#cleanup-afterwards)),
and on LSF the manager submits `clean` as its own 16-slot job. Run it yourself after a
skipped automatic cleanup, once the unfinished stage is finished and decided. A running or
noisy stage, or a stale decision, makes cleanup exit 41. Cleanup can run on any host and
ignores the verdict. See [session rules](../sessions.md) for prerequisites, retries and
stage exit codes.

## Scope and retained results

`clean` works on one session and never touches the store. It runs only after `decide` has
seen every stage's final state: it refuses (exit 41) while a stage is `running` (a killed job
must be run again to a final state first) or `noisy` (its measurement must be repeated),
before `decide`, and when a stage changed after the last `decide`, for example when
`unit-tests` was started later. It ignores the verdict. The automatic cleanup after `decide`
is also skipped while a stage the verdict requires has not started.

- **Deletes:** the evolved build, the source snapshots, the verifier and its cache, worker
  scratch directories, `oracle-jobs/`, the copied upstream test trees and their
  session-local `CARGO_HOME` crates, and verified quality and C6 prefix outputs larger than
  the policy's `output_retention_bytes`. Failing or unverified outputs are kept.
- **Keeps:** `run.json`, the archived profile, `harness-wheel/`, `stages/`, `evidence.json`,
  `decision.json`, `report.md`, the logs, `observations.jsonl`, `correctness.jsonl`,
  `clifford.jsonl`, `cost/` (with the monitoring evidence and diagnostics), `changed-tests.json`,
  every `job.json` and the snapshot manifests. The LSF records in `<session>.lsf/` are
  outside the session and never cleaned.

`clean` records every planned deletion in `clean.json` before deleting anything, and a
killed `clean` resumes from that list when run again. It prints the space freed. Afterwards
`decide` still works, and every other stage exits 41. To remove a session entirely, delete
its directory.

## Disk use and cleanup

Measured on a confirm A/A run on 2026-09-24 (8.3 GB in total, before the automatic cleanups
below existed):

| Item | Size | Needed later? |
| --- | ---: | --- |
| `source/target/debug`, from `cargo test` | 1.9 GB per build | No, once the unit tests are done |
| `source/target/release`, from the wheel build | 1.0 GB per build | No. The wheel is installed in `env/`, and `cargo test` uses the debug profile |
| `env/` | 312 MB per build | Yes, while stages still run |
| `upstream-*/tests.jsonl` | 308 MB each (3 per session) | Only the failures |
| `verifier/` | 291 MB per session | Yes, while stages still run |

With the automatic cleanups, a build takes about 330 MB, in the store for the baseline and
in the session for the evolved tree.

**Automatic, by the stage that made it:**

| Cleanup | Done by | When |
| --- | --- | --- |
| `source/target/release` | `compile` | Right after the wheel is installed and verified, for both builds. A store build gets its `READY` marker only after this |
| Rust test build | `unit-tests` | `CARGO_TARGET_DIR` is `upstream-<revision>/target`; it is deleted when the suite ends |
| `tests.jsonl` | `unit-tests` | Gzipped to `tests.jsonl.gz`, with failure text kept only for failed tests |

## After cleanup

`clean` affects only paths inside `S`. The baseline store, both original Qiskit source
folders and other sessions are outside its scope. It leaves `S` itself and its retained
evidence in place. Removing the entire session directory is a separate manual action.

`clean.json` records planned deletions and cleanup status; its fields are documented in
[output formats](../output-format.md#cleanjson). A cleanup left at `cleaning` must be
resumed with this command. No measurement stage may run while cleanup is incomplete or after
it is complete. [decide](decide.md) still works from retained evidence.
