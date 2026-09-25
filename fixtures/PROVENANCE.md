# Fixture provenance

Curated from Qiskit commit `0131cbbcc` using the pinned Qiskit 2.5.2 importer.
Canonical data is the contract. Comparison runs never call these constructors.
Qiskit-derived artifacts retain the Apache-2.0 license in LICENSE-QISKIT.txt.
QUEKO source: https://github.com/UCLA-VAST/QUEKO-benchmark (BSD-3-Clause).
The six RevLib artifacts with unresolved terms were replaced under plan section 3.8.
Each replacement is a distinct upstream synthesis-library construction; the manifest records the mapping.
Baseline roles, cost, and oracle coverage are validated on each new runner with A/A and known-outcome runs.

| Fixture | Upstream source | License / availability |
| --- | --- | --- |
| qft_n100 | test/benchmarks/utility_scale.py | Apache-2.0 |
| qft_cp_n14 | test/benchmarks/qft.py | Apache-2.0 |
| qft_full_n32 | test/benchmarks/utils.qft_circuit | Apache-2.0 |
| qft_cp_n8 | test/benchmarks/qft.py | Apache-2.0 |
| qft_full_n64 | test/benchmarks/utils.qft_circuit | Apache-2.0 |
| qft16_cancel | test/benchmarks/transpiler_qualitative.py | Apache-2.0 |
| square_heisenberg_n100 | test/benchmarks/utility_scale.py | Apache-2.0 |
| trotter_chain_n32 | test/benchmarks/utils.trotter_circuit | Apache-2.0 |
| trotter_chain_n16 | test/benchmarks/utils.trotter_circuit | Apache-2.0 |
| dtc_n100 | test/benchmarks/qasm/dtc_100_cx_12345.qasm | Apache-2.0 |
| qaoa_ba_n100_3reps | test/benchmarks/utility_scale.py | Apache-2.0 |
| qaoa_complete_n8 | test/benchmarks/utils.qaoa_circuit | Apache-2.0 |
| qaoa_complete_n16 | test/benchmarks/utils.qaoa_circuit | Apache-2.0 |
| qaoa_complete_n32 | test/benchmarks/utils.qaoa_circuit | Apache-2.0 |
| qv_n50_d50 | test/benchmarks/utility_scale.py | Apache-2.0 |
| qv_n14_d14 | test/benchmarks/transpiler_levels.py | Apache-2.0 |
| qv_n27_d27 | test/benchmarks/quantum_volume.py | Apache-2.0 |
| qv_n50_d20 | test/benchmarks/transpiler_levels.py | Apache-2.0 |
| qv_n14_d14_s10 | test/benchmarks/quantum_volume.py | Apache-2.0 |
| ripple_adder_10 | test/benchmarks/ripple_adder.py | Apache-2.0 |
| mcx_kg24_n6 | test/benchmarks/utils.mcx_circuit | Apache-2.0 |
| mcx_kg24_n5 | test/benchmarks/utils.mcx_circuit | Apache-2.0 |
| multiplier_h18_n32 | test/benchmarks/utils.multiplier_circuit | Apache-2.0 |
| mcx_kg24_n16 | test/benchmarks/utils.mcx_circuit | Apache-2.0 |
| ripple_adder_20 | test/benchmarks/ripple_adder.py | Apache-2.0 |
| adder_modular_v17_n8 | test/benchmarks/utils.modular_adder_circuit | Apache-2.0 |
| adder_modular_v17_n16 | test/benchmarks/utils.modular_adder_circuit | Apache-2.0 |
| adder_modular_v17_n32 | test/benchmarks/utils.modular_adder_circuit | Apache-2.0 |
| multiplier_h18_n16 | test/benchmarks/utils.multiplier_circuit | Apache-2.0 |
| adder_modular_v17_n6 | test/benchmarks/utils.modular_adder_circuit | Apache-2.0 |
| multiplier_h18_n20 | test/benchmarks/utils.multiplier_circuit | Apache-2.0 |
| bv_all_ones_n100 | test/benchmarks/utility_scale.py | Apache-2.0 |
| bv_all_ones_n16 | test/benchmarks/utils.bv_all_ones | Apache-2.0 |
| bv_all_ones_n50 | test/benchmarks/utils.bv_all_ones | Apache-2.0 |
| bvlike_n100 | test/benchmarks/utility_scale.py | Apache-2.0 |
| su2_circular_n89 | test/benchmarks/utility_scale.py | Apache-2.0 |
| ring_random_n8 | test/benchmarks/random_circuit_hex.py | Apache-2.0 |
| ring_random_n14 | test/benchmarks/random_circuit_hex.py | Apache-2.0 |
| su2_circular_n100 | test/benchmarks/utility_scale.py | Apache-2.0 |
| queko_bss_53 | test/benchmarks/queko.py | BSD-3-Clause (QUEKO) and Apache-2.0 (Qiskit adapter) |
| queko_bigd_20 | test/benchmarks/queko.py | BSD-3-Clause (QUEKO) and Apache-2.0 (Qiskit adapter) |
| queko_bntf_54 | test/benchmarks/queko.py | BSD-3-Clause (QUEKO) and Apache-2.0 (Qiskit adapter) |
| long_2q_sequence | test/benchmarks/transpiler_levels.py | Apache-2.0 |
| a2a_qft_n16 | test/benchmarks/transpiler_ft.py | Apache-2.0 |
| a2a_trotter_n16 | test/benchmarks/transpiler_ft.py | Apache-2.0 |
| a2a_qaoa_n16 | test/benchmarks/transpiler_ft.py | Apache-2.0 |
| a2a_multiplier_n16 | test/benchmarks/transpiler_ft.py | Apache-2.0 |
| a2a_adder_modular_n16 | test/benchmarks/transpiler_ft.py | Apache-2.0 |
| a2a_mcx_n16 | test/benchmarks/transpiler_ft.py | Apache-2.0 |
| single_h | test/benchmarks/transpiler_benchmarks.py | Apache-2.0 |
| cancel_2q | test/benchmarks/transpiler_benchmarks.py | Apache-2.0 |
