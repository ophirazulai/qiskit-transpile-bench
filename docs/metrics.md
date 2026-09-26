# How metrics and verdicts are calculated

[Documentation index](README.md)

This page follows one number from a compiled circuit to the final verdict. The code lives in
`src/qtb/metrics/` (per-circuit metrics), `src/qtb/evaluator/statistics.py` (quality
estimators), `src/qtb/evaluator/cost.py` (time and memory) and `src/qtb/evaluator/__init__.py`
(guards and verdict). None of it imports Qiskit.

## 1. Per-circuit metrics: `D2` and `N2`

Both are computed from the canonical output file the worker exports, using the list of native
two-qubit gate names stored in the frozen target (`native_2q_names`, for example `["cz"]`).

- **`N2`**: the number of operations whose name is in `native_2q_names` and that act on
  exactly two qubits.
- **`D2`** (the objective): the length of the longest dependency path when only those
  operations count. Every operation is still a node on all of its qubit and classical-bit
  wires. Counted operations weigh 1, and all others (single-qubit gates, barriers,
  measurements) weigh 0 but still synchronize their wires.

```python
level, n2 = {}, 0                      # wire -> counted layers so far
for name, qubits, clbits, params, payload in ops:          # circuit order
    wires = [("q", q) for q in qubits] + [("c", c) for c in clbits]
    counted = name in native_2q_names and len(qubits) == 2
    new = max(level.get(w, 0) for w in wires) + counted
    for w in wires:
        level[w] = new
    n2 += counted
D2 = max(level.values())
```

This matches `QuantumCircuit.depth(filter_function=...)` semantics, but it is computed by the
harness from the exported file, so the candidate cannot change its own score by changing
`depth()`.

**Example.** On four qubits, `cz(0,1); cz(2,3); rz(1); cz(1,2); cz(0,1)` has `N2 = 4` and
`D2 = 3`: the first two gates share a layer.

**Why `D2` and not total depth.** `D2` approximates duration when two-qubit gates dominate, and
`N2` tracks the dominant gate error. Total depth counts a layer of virtual, zero-duration `rz`
gates the same as a layer of entangling gates, so it can fall without any physical benefit.
A routing SWAP translated into three `cz` adds 3 to `N2` but adds to `D2` only on the critical
path. That is why a candidate could buy depth with extra gates, and why `N2` is guarded.

`D2` is undefined for dynamic circuits (control flow). Those appear only in the correctness
fixtures, never in scored cases.

The same streaming pass also runs the **C0 structural check** (`StructuralChecker`) and
computes the SHA-256 of the output. See [verifier.md](verifier.md#c0-structure-every-output).

## 2. One case: the per-case ratio

For a positive metric `m` (`D2` or `N2`), case `c` and the 100 seeds `s` of block B0:

```text
d(c, s)       = ln m_evolved(c, s) − ln m_baseline(c, s)       (paired by seed)
case_ratio(c) = exp( mean_s d(c, s) )
              = geomean_s m_evolved(c, s) / geomean_s m_baseline(c, s)
```

Logarithms are used because cases differ in size by orders of magnitude, effects are
multiplicative, and a ratio and its inverse should count symmetrically. A ratio of 0.98 means
a 2% reduction in the geometric mean.

## 3. A panel: the score and its paired standard error

A panel is a set of cases with frozen weights `w_c` that sum to one (the scored panel, a basis
panel, a family, a level). For each seed, collapse the panel to one number:

```text
delta_s   = Σ_c w_c · d(c, s)                  weighted log change at seed s
ln(score) = mean_s delta_s
score     = exp(ln(score))
SE        = sample_sd(delta_s) / sqrt(|S|)     (ddof = 1, |S| = 100)
```

Because each seed's delta already mixes all cases, the standard error keeps whatever
correlation exists between cases and between the two revisions at a shared seed. It does not
assume independence. Implemented in `paired_panel()`.

**Worked example** (three cases, weights 1/3, four seeds; `tests/test_statistics.py`
reproduces these digits):

| Case | Baseline, seeds 1–4 | Evolved, seeds 1–4 | `case_ratio` |
| --- | --- | --- | --- |
| A | 400, 420, 380, 410 | 390, 400, 385, 395 | 0.9757 |
| B | 1500, 1550, 1480, 1600 | 1490, 1500, 1500, 1540 | 0.9841 |
| C | 1850, 1800, 1900, 1820 | 1800, 1810, 1830, 1790 | 0.9812 |

`delta_s = (−0.01980, −0.02535, −0.00368, −0.03070)`, so `ln(score) = −0.01988`,
`score = 0.9803`, `SE = 0.00584` and `ln(score) + 2·SE = −0.00820 < 0`. This counts as an
improvement.

### Weights

- **Iterations profile:** the three scored cases have weight 1/3 each. The basis panels use
  equal weights.
- **Confirm profile:** a tree. Each family gets 1/8, split equally by level, then size band,
  topology, basis, input group and variant (`hierarchical_weights()`). The weights are frozen
  in the manifest; `load_profile()` refuses scored weights that do not sum to 1.
- **Sub-panels** (a family, a level): restrict to member cases and renormalize.

## 4. Decision rules

| Rule | Inequality | Used for |
| --- | --- | --- |
| Improvement, iterations | `ln(D2 score) + 2·SE < 0` | `IA2/improvement` |
| Improvement, confirm | `ln(D2 score) + 2·SE < ln(0.99)` | `CA2/improvement` (at least 1% practical gain) |
| Breadth, confirm | ≥ 4 of 8 families with `ln(D2) + 2·SE < 0`, and every leave-one-family-out score < 1 | `CA3/breadth` |
| Regression guard | `ln(score) ≤ 3·SE` | `N2` of the scored panel, basis panels, confirm family and level summaries |
| Per-case quality cap | `case_ratio ≤ 1.05` (seed-aggregated, not per seed) | Every scored and guard case, `D2` and `N2` |
| Per-case SE guard | `ln(case_ratio) ≤ 3·SE_case` | Guard cases outside a basis panel |
| Exact | Evolved `≤` baseline on every seed | Deterministic cases |
| Exact zero | Evolved `= 0` | Zero-baseline cases |
| Canary | Both revisions `=` the frozen constant on every seed | Canaries |

The multipliers (2.0 for improvement, 3.0 for guards), the practical ratio and the cap come
from `policy.json`.

**Why 3·SE for guards.** A profile has many guards. At 2·SE each would reject a neutral
candidate about 2.3% of the time, and about 45 guards (confirm) would reject a neutral candidate
most of the time. At 3·SE the per-guard rate is about 0.13%. The real, correlated family-wise
rate is not measured (section 5).

### Missing values and zeros

- A panel with any missing observation, or with a non-positive value in a log metric, is
  **incomplete**. Its record is `unresolved`, never `passed` or `failed`.
- Guards and improvement are evaluated only when `harness/roundtrip` and `audit/determinism`
  both passed. Otherwise every quality record is `unresolved`.
- Failures are never dropped from a denominator, and weights are never silently changed.

## 5. No per-machine calibration

Earlier versions compiled the baseline on two extra seed blocks to estimate the family-wise
false-rejection rate of the guards, and timed the baseline against itself to measure the
machine's cost noise. Version 4 of the profiles removed that phase (see
[design/remove-calibration-plan.md](../design/remove-calibration-plan.md)):

- The quality guards use their fixed multipliers only. Their combined false-rejection rate on
  a given runner is not estimated, and the report says so.
- The cost guards use the fixed thresholds of section 6. Whether they match a runner's noise
  is checked, if at all, by one manual A/A session (same source as baseline and evolved).
- Deterministic, zero-baseline and canary constants are checked on the B0 seeds only. A
  baseline that misses its own constant there gives `INCONCLUSIVE`.

## 6. Cost: time and memory

Cost has its own estimators. The quality seed estimator is never applied to cost.

```text
t(c, arm)     = median over rounds of ( median over timed calls in that round )
companion     = mean over seeds 0–19 of the above (arithmetic mean: users pay the average)
rss(c, arm)   = median over fresh processes of peak RSS
ln_panel      = Σ_c u_c · ln( t(c, evolved) / t(c, baseline) )   u_c = 1/|panel|
                (memory: equal weight per family)
```

- **Protocol** (`policy.json` → `measurement_protocol`): timing panels are measured in up to
  three regimes, each a fresh session: a `screen_rounds` screen (4), a `timing_rounds` full
  measurement (6 in the iterations profile, 10 in confirm) and a rerun at `rerun_multiplier`
  times the full count. The companion has 3 rounds per seed and memory 5 processes, each with
  a doubled rerun and no screen. A timing round is one fresh process per arm that loads,
  warms up (`warmups` = 1 call) and times every case of the panel in manifest order, timing
  each until at least `minimum_calls` (2) calls and `minimum_ns` (1 s) have accumulated; the
  companion and memory use one fresh process per arm and case. The two arms (baseline,
  evolved) run interleaved in random order within each round. Nothing else may run on the
  measured core. On LSF the stage holds nine exclusive physical cores, and a monitor checks
  every window of every worker for foreign CPU activity and involuntary preemption; the first
  failing window discards the invocation (`noisy`) and the manager measures again in a new
  job ([cost](workflow/cost.md#measurement-modes)). Run directly, the stage takes an
  exclusive runner lock and waits up to 5 minutes for the load average to fall below half
  the core count; if it does not, the result is `unresolved`.
- **Thresholds** (`policy.json` → `cost_thresholds`), the same for every runner:
  `panel_ratio` 1.03, `case_ratio` 1.10, `case_floor_ns` 25 ms, `case_floor_bytes` 32 MiB,
  `screen_fraction` 0.5. Every cost bundle records the digest of this block
  (`thresholds_id`), and a bundle judged under different thresholds is `unresolved`.
- **Guard:** a panel breaches if `ln_panel > ln(panel_ratio)`, or if any case has
  `ratio > case_ratio` **and** an absolute delta above its floor. The floor keeps jitter on
  millisecond cases (T1, T2, `preset_build`) from counting as a regression.
- **Screen:** the screen passes only when the candidate's `ln_panel` is within
  `screen_fraction × ln(panel_ratio)` with no per-case breach. A clear screen is a `passed`
  panel; anything else discards nothing but proceeds to the full measurement in a fresh
  session.
- **One rerun:** if the candidate breaches in the full measurement, the whole panel is
  measured once more with doubled rounds. A breach again → `failed`; a pass →
  `passed_on_rerun`.

Cost is its own stage, `cost`, because it needs quiet, exclusive cores and can take hours
of wall time. It runs after `correctness` and only when the quality gate is open
([quality gate](workflow/quality.md#the-quality-gate)): the candidate improved and every quality check passed, or the run is an A/A run
(both builds have the same ID) and every quality check passed. An A/A run never improves, but
its cost panels are the known-outcome check that timing and memory report no change. When
correctness found a failure, `cost` records `skipped` without measuring. Cost samples are
never taken from the baseline store: both arms are measured in the same session.

## 7. Change scope and stage coverage

A candidate can only `PASS` if a **verified** correctness check covers every transpiler stage
it changed (`evaluator/scope.py`):

1. The changed files are the difference between the two snapshots.
2. Each path is mapped to stages, per optimization level. Examples: `sabre` →
   layout, routing; `vf2` → layout, routing (+ optimization at level 3); two-qubit
   decomposition or unitary synthesis → init, translation, optimization; commutative
   cancellation → init, optimization. Tests, docs, release notes and lock files are ignored.
   Any other path under `qiskit/` or `crates/*/src/` counts as **all stages**, and so does
   any path the mapping does not recognize. There is no way to declare a scope by hand.
3. For every scored case, some verified check must cover all changed stages, for an applicable
   input domain, without having substituted a changed component. Otherwise
   `IA1/stage-coverage` (`CA1/...`) is `unresolved`.

The rule combines the quality checks (C0, C6, C1-lite) with the C7 Clifford checks of the
`correctness` stage, so `decide` computes `*1/stage-coverage` once both stages are complete.
If either has not run, for example because the quality gate is closed, the record is missing.

In practice: routing replay (C6) covers layout and routing on every scored output, so a
layout/routing-only change can `PASS`. Nothing verifies the optimization stage at scale for
100-qubit generic-angle circuits at levels 2–3, so a change there reaches at most
`INCONCLUSIVE` on the iterations profile. The confirm profile's C1-lite checks give exact
all-stage evidence on the small scored outputs.

## 8. From records to verdict

Every check produces a **constraint record**: `{id, kind, subject, result, ...}`.

- `kind`: `harness`, `correctness`, `guard`, `cost`, `improvement`, `completeness`
- `subject`: `evolved` (the candidate or the comparison) or `reference` (the baseline alone)
- `result`: `passed`, `failed`, `passed_on_rerun`, `unresolved`, `not_evaluated`

The set of required IDs comes from the policy, plus every panel the evaluator creates, plus
the cost panels the change scope requires, plus `*1/upstream` once the optional `unit-tests`
stage has started. It never depends on which records happen to exist. The verdict
(`evaluator.verdict()`) is decided in this order:

```text
any failed harness record                        → ERROR                 (exit 40)
any failed evolved record that is not improvement → CONSTRAINT_VIOLATION  (exit 20)
any failed reference record                       → INCONCLUSIVE          (exit 30)
any failed improvement record                     → NO_IMPROVEMENT        (exit 10)
every required ID present and passed              → PASS                  (exit 0)
otherwise (missing / unresolved)                  → INCONCLUSIVE          (exit 30)
```

A missing record can never produce `PASS`. There is no qualification record: a session ends
`PASS` when every required record passes. A/A runs and the known-outcome mutations remain the
way to validate the harness on a new runner
([known-outcome controls](validation.md#known-outcome-controls)), but they are practices, not
required records.

### Workflow conditions

[quality](workflow/quality.md#the-quality-gate) owns the gate that controls later stages.
[decide](workflow/decide.md#committed-evidence-and-required-stages) explains which committed
stages count, how unfinished stages impose `INCONCLUSIVE`, optional unit tests, and the
baseline-preflight exception. The estimator and record precedence above stay the same;
the workflow determines which evidence exists.

## Diagnostics that never enter a decision

Total depth, pipeline fingerprints (pass names and search budgets per stage), per-pass times,
routing SWAP counts from replay, worst seeds and `U_instance` are recorded for review only.
