# 3. correctness

[Documentation index](../README.md) · [← quality](quality.md) · [unit-tests
→](unit-tests.md)

Check semantic correctness and API contracts, using the pinned trusted verifier.

## Run this step

```bash
uv run qiskit-transpile-bench correctness --results-root "$S"
```

`S` is the session directory chosen at `compile`. `quality` must be complete. A closed gate
records `skipped`; an open gate permits work on a busy host. See [session
rules](../sessions.md) for prerequisites, retries and stage exit codes.

## What runs

1. Read or compute the baseline's frozen C1–C5 suite, API contracts and C7 Clifford variants.
2. Write `baseline/preflight`. If the baseline has a proven failure, stop: the evolved half
   is not checked. `decide` sets aside quality evidence and reports `INCONCLUSIVE`.
3. Otherwise run the same checks on the evolved build.

| Check | Contract |
| --- | --- |
| C1 | Exact operator equivalence, including coherent-control phase checks |
| C2 | Layout, ancillas and measurement semantics |
| C3 | Dynamic circuits with exact branching |
| C4 | Symbolic parameters and declared numeric bindings |
| C5 | Scheduling validity |
| API | Input immutability, pass-manager reuse, batch order and expected errors |
| C7 | Clifford variants at full width; prefix mode on iterations, prefix and full modes on confirm |

The frozen C1–C5 suite has 480 configurations × 5 seeds = 2,400 compiles per revision. The
[verifier reference](../verifier.md) explains each oracle, its input domain, tolerances,
coverage and limits. A proven candidate mismatch becomes a failed correctness record; a
check that cannot decide is unresolved and never counts as a pass.

## Outputs and baseline reuse

The stage owns `correctness.jsonl`, `clifford.jsonl` and `stages/correctness/`. The baseline
half is stored only when every baseline check is decisive (`verified` or `mismatch`) and no
worker failed or timed out. Stored failures can therefore identify a broken baseline in
later sessions. A/A sessions always compute both halves. See [store keys](../store.md) and
[check results](../verifier.md#check-results).

The semantic verifier batches distinct outputs and caches decisive results in the session's
`verifier-cache/`. Five seeds produce about 850 distinct outputs out of 2,400 compiles per
revision; seeds that reproduce an output reuse its check.

## Retry and continue

A retry resets stage evidence and truncates `correctness.jsonl` and `clifford.jsonl` before
restarting the suites. Verifier cache hits keep repeated checks cheap and rows do not
accumulate duplicates.

When the stage completes without a failure, run [cost](cost.md) on a quiet host. An
unresolved correctness check can still leave the eventual verdict `INCONCLUSIVE`; it is not
proof of correctness. When a correctness record failed, `cost` records `skipped`. The
optional [unit-tests](unit-tests.md) stage is independent and can run in parallel. Only
[decide](decide.md) turns these findings into a verdict: a completed stage exits 0 even when
it found a mismatch.
