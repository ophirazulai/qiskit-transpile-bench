# The iterations profile

`iterations-profile` is the default profile. It is the **fast inner loop**: small enough to
run after every change, but with enough seeds to separate a real depth improvement from seed
noise. When a change looks good here, confirm it once with the broader
[confirm profile](confirm-profile.md).

```bash
uv run qiskit-transpile-bench compare --baseline /path/to/baseline --evolved /path/to/evolved
```

Files: `profiles/iterations-profile/manifest.json` (the workload) and
`profiles/iterations-profile/policy.json` (thresholds and measurement protocol). Both are
version 1 and marked `unqualified`.

## What it asks

> On three large, well-known 100-qubit circuits compiled at optimization level 2 onto a
> 193-qubit heavy-hex device with a `cz` entangler, is the evolved revision's native
> two-qubit depth (`D2`) lower than the baseline's by more than two standard errors, with
> no correctness failure, no quality regression on other bases, no lost simplification,
> and no compile-time regression?

## Workload: 34 cases

| Role | Count | What they are |
| --- | ---: | --- |
| Scored | 3 | The three circuits below on `heavy_hex_d9_cz`, level 2, 100 seeds each, weight 1/3 each |
| Guard (basis) | 6 | The same three circuits on `heavy_hex_d9_cx` and `heavy_hex_d9_ecr`, 100 seeds each |
| Canary | 3 | Cases whose `D2`/`N2` are constant at the baseline, 10 seeds each |
| Timing | 19 | The compile-time panel T1–T19 |
| Timing (preset) | 3 | Time to build the level-2 preset pass manager for each heavy-hex target |

Quality work per revision: 9 cases × 100 seeds + 3 canaries × 10 seeds = **930 compiles**,
plus two truncated compiles per seed for routing replay on the scored and basis-guard cases.

### The three scored circuits

All three are 100 qubits, measurement-free, with numeric angles. They come from Qiskit's own
`test/benchmarks/utility_scale.py`.

| Input group | Family | What it is | What it stresses |
| --- | --- | --- | --- |
| `qft_n100` | G1 | Quantum Fourier transform, 10,050 `cx` | Dense, all-to-all interaction on a sparse device. Many near-zero rotations, so simplification matters as much as routing |
| `square_heisenberg_n100` | G2 | Trotterized Heisenberg model on a 10×10 lattice, 2,160 `cx` | A degree-4 lattice that does not embed in a degree-3 device. Repeated local patterns exercise cancellation and 2-qubit block resynthesis |
| `qaoa_ba_n100_3reps` | G3 | Three QAOA layers on a 100-node Barabási–Albert graph, 1,176 `cx` | An irregular graph with hub nodes that serialize gates and create routing bottlenecks |

### The target

`heavy_hex_d9_{cz,cx,ecr}`: the distance-9 heavy-hexagon lattice with 193 physical qubits,
224 undirected couplings (the two-qubit gate is defined in both directions), and at most three
neighbors per qubit. A 100-qubit circuit therefore leaves 93 spare qubits. The three files
differ only in the entangling gate. They were generated once from `GenericBackendV2` with a
fixed seed and frozen as data, including synthetic error and duration values, because layout
scoring reads them. `cz` is the primary (scored) target.

### Canaries

A canary has a known constant output at the baseline. It catches a simplification that a
candidate silently loses (or unexpectedly gains) outside the scored cases.

| Case | Expected `D2` = `N2` | Why it is constant |
| --- | ---: | --- |
| `su2_circular_n100` on `heavy_hex_d9_cz`, level 2 | 300 | A 100-qubit ring embeds perfectly in heavy-hex, so VF2 finds a perfect layout and the seed is never used |
| `long_2q_sequence` on `rochester_53_u`, level 2 | 3 | Two-qubit block resynthesis collapses a 3,505-gate, two-qubit sequence into one decomposition |
| `long_2q_sequence` on `rochester_53_u`, level 3 | 3 | Same |

Both revisions must match the constant exactly on all 10 seeds. If the baseline misses, the
reference is broken (`INCONCLUSIVE`). If only the evolved revision misses, in either direction,
the canary is `unresolved`, which also gives `INCONCLUSIVE`: the change is unexplained and
needs human review.

### Timing panels

| IDs | Panel | Input | Constraints | Level | Mode |
| --- | --- | --- | --- | --- | --- |
| T1, T2 | `timing` | `single_h` (one `h`), `cancel_2q` (six gates that cancel) | Loose 27-qubit coupling map + basis list | Revision default | `timing_e2e` |
| T3–T6 | `timing` | `qv_n14_d14` (98 random 2-qubit unitaries) | `melbourne_14` target | 0, 1, 2, 3 | `timing_e2e` |
| T7–T10 | `timing` | `long_2q_sequence` | Loose Rochester map, legacy `u1,u2,u3,cx,id` basis | 0, 1, 2, 3 | `timing_e2e` |
| T11, T14, T17 | `timing` | The three scored circuits on `cz` | Heavy-hex target | 2 | `timing_reuse` |
| T12, T13, T15, T16, T18, T19 | `timing-basis` | The three scored circuits on `cx` and `ecr` | Heavy-hex targets | 2 | `timing_reuse` |
| preset/cx, cz, ecr | `preset` | — | Heavy-hex targets | 2 | `preset_build` |

`timing_e2e` times a whole `transpile()` call, including option handling and target
construction. `timing_reuse` builds the pass manager first, then times only `pm.run()`. T1–T2
expose fixed per-call overhead that large circuits hide. T3–T10 cover every optimization level.
T11, T14 and T17 time the scored workload itself.

Which panels are measured follows the change scope ([metrics.md](metrics.md#7-change-scope-and-stage-coverage)):

| Panel | Measured | Guarded |
| --- | --- | --- |
| `timing` | Always | Always |
| `timing-basis` | When the change can reach translation or optimization, or the scope is unknown | Whenever measured |
| `preset` | Always | When the change could affect preset assembly or target handling, or the scope is unknown |
| `companion` | When the change can reach layout or routing, or the scope is unknown | Whenever measured |

The `cx`/`ecr` twins repeat the `cz` cases' layout and routing on the same coupling map; only
the basis translation and the two-qubit resynthesis differ. A layout or routing change is
therefore timed on the `cz` cases and, through the **multi-seed companion panel**, on the three
`cz` cases over seeds 0–19 (three rounds per seed). The companion exists because one fixed seed
times only one search path.

**How a panel is measured.** Every round is one fresh process per arm (baseline, control,
evolved) that loads, warms up and times each case of the panel in manifest order, with the
arms in random order. A case's time in a round is the median of its timed calls (at least 2
calls and 1 s); the case time is the median over rounds. The panel starts with a 4-round
**screen**: if the control arm is clean and the candidate already sits inside the noise band
of the full 6-round measurement with no per-case breach, the panel stops. Otherwise it is
measured in full in a fresh session, and a candidate-only breach triggers one fresh 12-round
rerun that decides. The report names the regime each panel ended in.

## Acceptance rules (IA1–IA6)

All rules compare against the baseline. The formulas are in [metrics.md](metrics.md).

| Rule | Constraint record IDs | Requirement |
| --- | --- | --- |
| IA1 correctness | `IA1/C0`, `IA1/C1-C5`, `IA1/C6`, `IA1/C7`, `IA1/upstream`, `IA1/stage-coverage` | Every output is legal (C0). The C1–C5 suite passes for both revisions. Routing replay verifies every scored and basis-guard output. Clifford variants verify. The baseline's upstream Python tests and the Rust tests show no new failures. Every changed stage is covered by a verified check |
| IA2 improvement | `IA2/improvement` | Primary `cz` panel: `ln(D2 score) + 2·SE < 0` |
| IA3 guards | `IA3/primary/N2`, `IA3/primary/D2`, `IA3/cx/D2`, `IA3/cx/N2`, `IA3/ecr/D2`, `IA3/ecr/N2` | Each panel: `ln(score) ≤ 3·SE` |
| IA4 caps and canaries | `IA4/cap/<case>/<metric>`, `IA4/exact/<canary>` | No scored or guard case has a seed-aggregated ratio above 1.05 for `D2` or `N2`. Canaries equal their constants |
| IA5 cost | `IA5/timing`, `IA5/timing-basis`, `IA5/preset`, `IA5/companion` | Panel log time ratio within calibrated A/A noise, no per-case breach (> 10% and above the noise floor), and the control arm is clean |
| IA6 completeness | `IA6/completeness` | All 2 × (9 × 100 + 3 × 10) observations are present. Determinism audit passed |

Also required: `harness/roundtrip`, `harness/qualification`, `baseline/preflight`,
`audit/determinism`, `calibration/quality`, `calibration/cost`.

Policy values (`policy.json`): improvement multiplier 2.0, guard multiplier 3.0, practical
ratio 1.0 (any improvement beyond noise counts), quality cap 1.05, RNG seed 20260924,
upstream test budgets of 4 h (Python) and 3 h (Rust). Timing: 4 screen rounds, 6 full rounds,
rerun multiplier 2, 30 calibration rounds, 1 warm-up, at least 2 timed calls and 1 s per round.

## Statistical power

With the measured seed-to-seed spread of `ln D2` (about 4–9% per circuit), the paired
standard error of the three-circuit score over 100 seeds is about 0.56%. A true 1.1% depth
reduction passes about half the time. About 1.6% is needed to pass four times in five. These
are planning estimates from the design; measure the actual `SE` on your runner (it is printed
in `report.md`).

**Repeated attempts:** a neutral change passes the improvement test about 2.3% of the time.
After 20 edit-and-compare rounds on the same profile, the chance of at least one lucky `PASS`
is about a third. This is why the confirm profile exists: it contains 35 input groups that
this loop never sees.

## Budget

On the development machine (serial, Apple M1 Max): about 10 CPU-minutes of quality compiles
per revision, roughly doubled by routing replay. The cost panels take about 10–15 minutes on
an exclusive machine when the screen is clear (16 cases × 4 rounds × 3 arms, in 12 processes),
up to about 40 minutes with the full count and a rerun, plus about 20 minutes for the
companion when required. These are design estimates, not measured runs. The first run against
a baseline also pays for calibration (three seed blocks of baseline quality, a 300-seed role
audit, and 30 A/A rounds of every cost panel from two baseline builds, about an hour). Later
runs reuse it for 30 days. Building the Qiskit environments dominates the first run; see
[environments.md](environments.md#how-long-it-takes).

## Declared coverage gaps

The manifest lists what the workload does not cover, and every report repeats it. The
iterations profile has one circuit per family (G1–G3), one target topology (heavy-hex), one
level (2) for scored cases, and all-zero input states. A `PASS` here is a claim about these
three circuits only.
