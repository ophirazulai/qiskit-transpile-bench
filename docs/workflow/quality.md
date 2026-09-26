# 2. quality

[Documentation index](../README.md) · [← compile](compile.md) · [correctness
→](correctness.md)

Measure circuit quality, check the compiled outputs and compute the gate for the expensive
stages.

## Run this step

```bash
uv run qiskit-transpile-bench quality --results-root "$S"
```

`S` is the session directory chosen at `compile`. `compile` must be complete. This stage can
use a busy host. See [session rules](../sessions.md) for prerequisites, retries and stage
exit codes.

## Collect quality observations

The workload is every profile case whose role is neither timing nor memory. Each revision
compiles the case on its frozen B0 seeds, normally 0–99. Baseline observations can come from
the [store](../store.md); the evolved half always runs in this session. A/A sessions collect
both halves independently.

The coordinator computes native two-qubit depth (`D2`) and gate count (`N2`) from the
exports. `D2` is the objective; `N2`, basis panels, per-case caps and exact cases are
guards. [Metrics](../metrics.md) defines the estimators, weights and thresholds.

| Check | Scope |
| --- | --- |
| C0 legality and layout | Every quality output |
| C6 routing replay | Every scored and basis-guard seed; ten B0 seeds of other static guards |
| C1-lite | Confirm: eligible scored outputs with at most 25 logical qubits, first ten seeds |
| Completeness | All required observations and checks must exist |

See [verifier checks](../verifier.md#the-checks) for the contracts and eligibility rules. Up
to nine seed batches compile at once, limited by the [worker count](../sessions.md#workers).
The compile times of these concurrent jobs never enter the cost verdict.

## Determinism audit

Quality observations must be reproducible. After the B0 block, at least 5% (minimum 10) of
observations per revision are recompiled, half of them with `PYTHONHASHSEED=1`. Each must
reproduce the same output hash and layout. If one does not, `audit/determinism` is
`unresolved`, which makes every quality verdict `unresolved`. The matching baseline quality
entries in the store are then marked invalid (`invalidated.json`) and never reused; stored
correctness and unit-test results are kept. Evolved observations are never stored.

## The quality gate

At the end of `quality`, the gate decides whether the expensive stages run. It is stored in
`stages/quality/state.json` with its reason, and cannot be overridden.

| Gate | When | Then |
| --- | --- | --- |
| `improved` | Every quality check passed (round-trip, audit, C0, C6, C1-lite, guards, caps, completeness) and every improvement record passed | `correctness`, `unit-tests` and `cost` run |
| `aa` | Both builds have the same ID (an A/A session) and every quality check passed | They run as a check of the harness; the cost panels are the known-outcome check |
| `closed` | Anything else: no improvement, an unresolved improvement, or a failed or unresolved quality check | They record `skipped` with the reason and exit 0 within seconds |

`cost` checks once more: if `correctness` found a failure, `cost` records `skipped`.

**What `NO_IMPROVEMENT` checks.** Both revisions built and compiled identical inputs, no
quality check failed, and the candidate showed no gain beyond seed noise. **It does not
check correctness.** With the gate closed, the C1–C5 suite, the API contracts, the C7
Clifford checks, the upstream tests and the cost panels never run, so an incorrect candidate
without an improvement also ends `NO_IMPROVEMENT`. The report says so: "Correctness, unit
tests and cost not checked: the quality gate is closed". It also says whether the baseline's
correctness is known from the store. A failed quality check (C0, C6 or a guard) still gives
`CONSTRAINT_VIOLATION`, without the correctness suite.

If the baseline itself fails its correctness checks, the evolved revision is not checked,
the quality evidence is set aside, and the verdict is `INCONCLUSIVE`. Without an
improvement, correctness never runs, so a broken baseline is found by the first session
against it that gets through the gate.

## Outputs and retries

The stage writes `observations.jsonl` and its own state, evidence and progress log under
`stages/quality/`. The committed state records `gate` and `gate_reason`. Missing required
quality records, `not_evaluated` checks and unresolved checks close the gate. The gate
cannot be overridden. Identical build IDs are labeled `aa` even if a noisy estimate appears
improved.

A retry skips seeds already recorded in `observations.jsonl`, then reruns the audit and
aggregate checks. A finished stage cannot be rerun in the same session.

## Expected work

Iterations quality is roughly 930 compiles per revision, about ten CPU-minutes before
routing replay, which roughly doubles the work. Confirm quality is roughly 15,000 compiles
per revision, about 3.5 CPU-hours for the candidate once baseline results are stored. These
are workload estimates, not guaranteed wall times; concurrency and the host matter.

## Continue or stop

For `improved` or `aa`, run [correctness](correctness.md) and optionally [unit
tests](unit-tests.md). Those two stages can run at the same time. For `closed`, run
[decide](decide.md) to report the quality findings. Calling the later stages will record
`skipped` and exit 0.
