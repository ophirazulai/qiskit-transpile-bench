# The confirm profile

`confirm-profile` is the **broad check**. Run it once, when a change looks good on the
[iterations profile](iterations-profile.md), to find out whether the gain holds beyond the
three circuits you tuned on.

```bash
uv run qiskit-transpile-bench compare --baseline /path/to/baseline --evolved /path/to/evolved \
    --profile confirm-profile
```

Files: `profiles/confirm-profile/manifest.json` and `profiles/confirm-profile/policy.json`.
Version 1, marked `unqualified`.

> **Do not tune on this profile.** The workload is public and fixed, with no held-back part.
> Editing a candidate and rerunning confirm until it passes turns it into a second tuning
> loop. Every report prints how many decisions the results root already holds for this
> manifest, so overuse is visible.

## What a `PASS` claims

Native two-qubit depth improved across eight circuit families, optimization levels 0–3,
heavy-hex and grid-class targets, and the `cx` and `cz` bases (`ecr` guarded), on the fixed
workload drawn from Qiskit's in-tree benchmark suite (`test/benchmarks/`), within the gate
count, compile time, memory and correctness limits. It says nothing about circuits outside
this workload.

## Workload: 271 cases

| Role | Count | Purpose |
| --- | ---: | --- |
| Scored | 133 | 38 input groups × levels × targets; 100 seeds each; weights sum to 1 |
| Guard | 19 | Per-case regression guards, including the `cx`/`ecr` basis guards of the three 100-qubit circuits |
| Deterministic | 48 | Seed-blind cases compared exactly, seed by seed; any increase fails |
| Zero-baseline | 4 | Cases whose baseline `D2` = `N2` = 0; any non-zero value fails |
| Canary | 5 | Constant cases that must equal a frozen value |
| Timing | 53 | T1–T19 and the preset panel from the iterations profile, plus a 31-case confirm timing panel |
| Memory | 9 | Peak RSS of one compile in a fresh process |

Quality work per revision is about 15,000 compiles.

### Families

Every family carries exactly 1/8 of the score.

| ID | Family | Scored input groups |
| --- | --- | --- |
| G1 | Quantum Fourier transform | `qft_n100`, `qft_full_n32`, `qft_full_n64`, `qft_cp_n8`, `qft_cp_n14` |
| G2 | Hamiltonian simulation | `square_heisenberg_n100`, `dtc_n100`, `trotter_chain_n16`, `trotter_chain_n32` |
| G3 | QAOA | `qaoa_ba_n100_3reps`, `qaoa_complete_n8`, `qaoa_complete_n16`, `qaoa_complete_n32` |
| G4 | Quantum volume | `qv_n50_d50`, `qv_n50_d20`, `qv_n27_d27`, `qv_n14_d14`, `qv_n14_d14_s10` |
| G5 | Reversible logic and arithmetic | `ripple_adder_10`, `ripple_adder_20`, `multiplier_h18_n16`, `multiplier_h18_n32`, `adder_modular_v17_n8/n16/n32`, `mcx_kg24_n5/n6/n16` |
| G6 | Bernstein–Vazirani | `bv_all_ones_n16`, `bv_all_ones_n50`, `bv_all_ones_n100` |
| G7 | Variational ansatz | `su2_circular_n89`, `su2_circular_n100`, `ring_random_n8`, `ring_random_n14` |
| G8 | Routing challenges (QUEKO) | `queko_bigd_20`, `queko_bss_53`, `queko_bntf_54`, with explicit SABRE layout and routing |

Six RevLib/HWB inputs from the original design had unclear redistribution terms. They were
replaced by generated reversible circuits from Qiskit's `test/benchmarks/utils.py`; the manifest
records the substitutions under `replacements`. See [fixture provenance](../fixtures/PROVENANCE.md).

### Coverage dimensions

| Dimension | Values in the scored panel |
| --- | --- |
| Optimization level | 0 (38 cases), 1 (32), 2 (32), 3 (31) |
| Size band (logical qubits) | small 4–16 (59), medium 17–64 (53), large 65–100 (21) |
| Topology | heavy-hex (103), grid (30) |
| Native 2-qubit basis | `cx` (104), `cz` (29) |
| Constraint form | frozen `Target` (70), loose basis list + coupling map (63) |
| Targets | `mumbai_27`, `heavy_hex_d9_cz`, `rochester_53`, `melbourne_14_u`, `mumbai_27_loose`, `melbourne_14` (directed), `rochester_53_u`, `grid_5x5_u`, `grid_7x7_u`, `tokyo_20`, `sycamore_54` |

Inputs whose interaction graph is a path or ring (Trotter chains, DTC, random rings, circular
SU2 at 100 qubits) embed perfectly at levels 1–3, so every seed gives the same result there.
They are scored at level 0 only and are deterministic guards at levels 1–3.

### Weights

The weights form a tree. Each family gets 1/8. Inside a family, weight is split equally by
level, then size band, then topology, then basis, then input group, then variant. Adding more
inputs to one family therefore cannot outweigh the other seven. Two consequences are stated
openly: `square_heisenberg_n100` carries about 9.9% of the score and `su2_circular_n89` about
9.4%, because each is the only seed-sensitive input of its family at some levels. The
iterations profile's three circuits together carry 18.2%.

### Guard-only and exact cases

- **Deterministic** (48): QUEKO with default methods (VF2 finds the perfect embedding), the
  path- and ring-shaped inputs at levels 1–3, and six all-to-all Clifford + `rz` controls
  (`a2a_*` on `a2a_clifford_rz_16`). Compared exactly on 10 seeds.
- **Zero-baseline** (4): `bvlike_n100` and `qft16_cancel` at levels 2–3, where every
  entangling gate cancels.
- **Guards** (19): the three 100-qubit circuits on `cx`/`ecr`, `su2_circular_n89` at level 3
  (10 seeds), `multiplier_h18_n20` (20 seeds, the `hwb12` replacement), QUEKO at level 0,
  and others. Every guard must stay at or below the 1.05 per-case cap. Guards outside the
  basis panels must also satisfy `ln(ratio) ≤ 3·SE_case`.
- **Canaries** (5): `su2_circular_n100` and `long_2q_sequence` at several levels.

### Cost panels

| Panel | Cases | Mode |
| --- | --- | --- |
| `timing` | T1–T19, unchanged from the iterations profile | `timing_e2e` / `timing_reuse` |
| `preset` | Preset construction on the three heavy-hex targets | `preset_build` |
| `confirm-timing` | 31 cases: the heaviest scored input of each family (`qft_n100`, `square_heisenberg_n100`, `qaoa_ba_n100_3reps`, `qv_n50_d50`, `multiplier_h18_n32`, `bv_all_ones_n100`, `queko_bss_53`) at levels 0–3, and `su2_circular_n89` at 0–2 | `timing_e2e`, seed 0 |
| `memory` | 9 cases, the same heavy inputs plus `multiplier_h18_n20` at level 2; 5 fresh processes each | `memory` |
| `companion` | The same heavy inputs at level 2 over seeds 0–19 (only when layout or routing changed) | `timing_reuse` |

## Acceptance rules (CA1–CA6)

The broad rules replace the iterations rules. A confirm `PASS` does **not** also require the
three-circuit iterations score to improve.

| Rule | Records | Requirement |
| --- | --- | --- |
| CA1 correctness | `CA1/C0`, `CA1/C1-C5`, `CA1/C6`, `CA1/C7`, `CA1/C1-lite`, `CA1/upstream`, `CA1/stage-coverage` | As IA1, plus **C1-lite**: exact all-zero-input state or measurement checks on the first 10 seeds of every scored case with at most 25 logical qubits and no free parameters. An output whose simulated wire union exceeds 25 is recorded as `unverified` |
| CA2 improvement | `CA2/improvement` | `ln(D2 score) + 2·SE < ln(0.99)`, a practical 1% reduction beyond seed noise |
| CA3 breadth | `CA3/breadth` | At least 4 of the 8 families individually satisfy `ln(D2) + 2·SE < 0`, **and** removing any one family still leaves the score below 1 |
| CA4 guards | `CA3/primary/N2`, `CA3/cx/*`, `CA3/ecr/*`, `CA4/family/<G>/<metric>`, `CA4/optimization_level/<L>/<metric>`, `CA4/cap/...`, `CA4/exact/...` | Every family and level summary has `ln(score) ≤ 3·SE` for `D2` and `N2`, and so does overall `N2`. Per-case caps (1.05). Deterministic, zero-baseline and canary cases exact. Band, topology and basis summaries are reported only |
| CA5 cost | `CA5/timing`, `CA5/confirm-timing`, `CA5/memory`, `CA5/preset`, `CA5/companion` | Each panel within calibrated A/A noise, no per-case breach, clean control arm |
| CA6 completeness | `CA6/completeness` | Every observation present; determinism audit passed |

Report-only numbers:

- **`U_instance`**: a family-stratified cluster bootstrap over input groups (10,000
  replicates). It estimates how much the score depends on which circuits were chosen. It is
  printed, not tested, because several family/size cells have fewer than three groups.
- **Leave-iterations-out score**: the score recomputed without `qft_n100`,
  `square_heisenberg_n100` and `qaoa_ba_n100_3reps`. A gain that lives mostly in those three
  circuits improved the tuning loop, not the suite.
- **Decision count** for this manifest in the results root.

Policy differences from the iterations profile: `practical_ratio` is 0.99 (versus 1.0), and
the required set adds `CA3/breadth`, `CA1/C1-lite`, `CA5/confirm-timing` and `CA5/memory`.

## Statistical power

The score averages many roughly independent cases, so its paired standard error over 100
seeds is about 0.2%: roughly three times smaller than the iterations profile's. A real gain
that passed iterations has a good chance here, and a lucky one very little. What limits the
confirm profile is the number of independent inputs, not the number of seeds. That is why
`U_instance` is reported.

## Budget

From the design probe on an Apple M1 Max, serial:

| Item | Cost |
| --- | --- |
| Quality compiles | About 15,000 compiles and 87 CPU-minutes per revision; routing replay roughly doubles this |
| C1-lite | Provisional; one 23-qubit `ripple_adder_10` check took about 4 minutes in validation |
| One decision, baseline cached | About 3.5 CPU-hours of quality work for the candidate; twice that the first time a baseline is used |
| Cost panels | Confirm timing about 1.3 h, T1–T19 about 1 h, memory about 15 min, companion about 20 min, all on an exclusive machine |

Quality is currently orchestrated serially. Plan for a long first run, and see
[environments.md](environments.md) for build time.

## Declared coverage gaps

Listed in the manifest (`coverage_gaps`) and repeated in every report:

- The upstream suite has no line target.
- G2, G3 and G6 appear on only one sparse topology class.
- G7 has no scored level-3 case.
- Some family/size cells have fewer than three independent groups.
- G4, G5 and G8 have no large-band scored inputs.

The design plan (section 3.7) lists what the next manifest version should add first.
