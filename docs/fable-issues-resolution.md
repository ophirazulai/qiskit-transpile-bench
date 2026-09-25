# Resolution of `fable-issues-bugs.md`

Reviewed against the working tree on 2026-09-24. Each numbered finding was checked separately. The table distinguishes code fixes from findings that were stale or still require controlled-runner evidence. Rows 7, 8, 11, 12, 20, 24, 26 and 29 were updated on 2026-09-25, when `compare` was split into separate stages with a baseline store and qualification was removed.

| # | Disposition | Result |
| --- | --- | --- |
| 1 | Fixed | C0 accepts Qiskit's implicit instructions for loose targets and keeps explicit targets strict. Frozen measured fixtures and a regression test confirm the issue. |
| 2 | Fixed | Known non-source changes no longer widen pipeline scope; unknown source changes remain conservative. |
| 3 | Fixed | C7 prefix and full results now contribute to stage coverage under their recorded variant contract. |
| 4 | Fixed | Timed-out batches retry later seeds. Abnormal exits record later seeds as not attempted, with unresolved completeness evidence. |
| 5 | Partially resolved | Worker compiles now record duration and `tools/freeze_timeouts.py` can produce a versioned timeout manifest only from complete baseline measurements. Shipped 120-second values remain provisional until controlled-runner measurements, including `multiplier_h18_n20`, are available. |
| 6 | Fixed | Upstream Python and Rust test budgets are versioned policy settings (four and three hours). They have not been calibrated by full upstream runs here. |
| 7 | Fixed, since superseded | `evaluate` and `--resume` were removed. `decide` recomputes a session's verdict from its archived profile and committed stage evidence, also with a newer harness. A failed or interrupted stage resumes when run again, and refuses a harness that changed since `compile`. |
| 8 | Fixed | Stored baseline quality keys (formerly the quality cache) use the CPU identity of the `quality` host and omit non-quality case labels. Store entries own their output and job artifacts and verify the stored output hash. The measurement protocol remains in the key as required by plan §9.3. |
| 9 | Fixed | Calibration reuse ignores reporter, CLI and wheel changes while retaining fingerprints for calibration-relevant code and profile inputs. |
| 10 | Fixed | Missing, added or wrong-wire measurements return `mismatch` rather than `unverified`. |
| 11 | Fixed | Identical baseline/evolved builds bypass stored baseline results so both arms compile independently. |
| 12 | Fixed, since superseded | The replay path and its separate output directory were removed with `evaluate`. `decide` rewrites only the session's `evidence.json`, `decision.json`, `report.md` and `progress.log`; it never changes stage evidence or `run.json`. The cited old replay files were absent. |
| 13 | Fixed | C6 replay follows the scored, basis-guard and ten-seed static-guard scope, and skips calibration blocks. |
| 14 | Fixed | Companion timing batches its 20 seeds into one worker per case, round and arm. Fixed-seed panels retain their process protocol. |
| 15 | Fixed | Preset timing remains measured but gates the verdict only for relevant or unknown change scope. |
| 16 | Not applicable | This checkout has no `exclusions.json` or runtime exclusion reader, so no exclusion input is missing from profile identity. |
| 17 | Fixed | Level-3 C6 records layout disagreement and the reason equality was skipped. |
| 18 | Partially resolved | C5 now rejects unexplained gaps between operations. The exported schedule has no independent final-gate duration witness, so that duration cannot be proved by this format. All 48 shipped C5 configurations verified. |
| 19 | Fixed | Missing confirm-summary inputs yield unavailable or unresolved summaries and still allow decision files to be written. |
| 20 | Fixed with a validation limit | C1-lite avoids full tensor copies and has an explicit memory/branch budget aligned with the 25-wire eligibility rule. An archived 23-wire case verified in 99 seconds; the full runner performance remains to be measured on a controlled runner. |
| 21 | Fixed | T1/T2 have the correct timing family and grade-A provenance in version-2 manifests. Mumbai's heavy-hex topology was already correct. |
| 22 | Fixed | Quality batch size and cost warmup/minimum settings now come from the validated policy and are recorded in cost bundles. |
| 23 | Fixed | Arm interleaving seeds derive from run/panel identity and are archived with bundles. |
| 24 | Fixed | Large verified C6 prefix outputs are pruned by `clean`, whatever the verdict, after hashes and job provenance are recorded in `clean.json`. |
| 25 | Fixed | Decision and report include available objective, trade, marginal, zero-baseline, coverage, bootstrap and fingerprint detail. Exclusions are marked unavailable because no frozen exclusion artifact exists. |
| 26 | Fixed | Rust builds have a four-hour limit. Each build keeps its crates in its own Cargo home; there is no shared registry. The frozen Qiskit source recognizes the exported build flags. |
| 27 | Partly stale, partly fixed | There is no `calibrate` CLI command to double-snapshot. Incomplete preflight work is now recorded under the correct quality or cost calibration ID. |
| 28 | Fixed | A regular installed harness can reconstruct a deterministic wheel from verified installed package files; editable installs still need their source. |
| 29 | Partially resolved | Added C7 verdict mutation, archived synthetic replay and interrupted cost-panel restart tests. Existing tests cover self-grading and cache bypass. A full independent-build A/A run remains a controlled-runner validation step in `docs/known-outcome-validation.md`. |

There is no qualification record any more: a session can end `PASS` when every required record passes. Validation on a controlled runner remains a practice ([implementation-status.md](implementation-status.md#validation-on-a-new-runner)). In particular, finding 5's frozen timeouts and finding 29's full A/A run still need measured evidence.
