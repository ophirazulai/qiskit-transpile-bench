# The verifier and correctness checks

## What the verifier is

The verifier (`verifier/qtb_verifier/`) is the **trusted semantic oracle**. It is a separate
Python process running in its own virtual environment with a fixed, released Qiskit:
**2.5.2**, pinned in `envs/verifier.lock`. That version is independent of both revisions under
test.

It exists because some correctness questions need real quantum-information tools:

- Are two circuits the same operator?
- Do two circuits prepare the same state?
- Do two circuits have the same Clifford tableau?

The revision under test cannot answer those about its own output. A candidate that changed
`Operator`, `Statevector` or `Clifford` could make a wrong circuit look right. So the
coordinator sends the candidate's exported output file and the frozen reference circuit to
the verifier, which rebuilds both inside Qiskit 2.5.2 and compares them.

Guarantees:

- It refuses to start unless `qiskit.__version__ == "2.5.2"`.
- It reads and writes only files: a `job.json` in, a `result.json` out
  (protocol `qtb-verifier/1`). No live objects cross process boundaries.
- It never sees the revision under test. It imports circuits through the same explicit
  codec (`qtb_worker.adapter.import_circuit`) in its own environment.
- Any exception becomes `status: "unverified"`. It never becomes a pass.

The verifier environment is built once per run in `results/runs/<run>/verifier/env`
(`pip install -r envs/common.lock -r envs/verifier.lock <harness wheel>`, about 45 seconds). See
[environments.md](environments.md).

Not everything runs in the verifier. The cheapest and most frequent checks (C0 structure and
C6 routing replay) are pure Python in the coordinator (`src/qtb/metrics/`). They need no
Qiskit, so they can run on every output.

## Check results

Every check returns one of:

| Status | Meaning |
| --- | --- |
| `verified` | Proven equal / legal under the stated contract |
| `mismatch` | Proven wrong. Always blocks: a baseline mismatch → `INCONCLUSIVE`, an evolved mismatch → `CONSTRAINT_VIOLATION` |
| `unverified` | Could not be decided (too wide, non-Clifford angle, unsupported construct, oracle error). Never counts as a pass |

Each record also names the oracle, the stages it covers (`covers`), the input domain
(`all_inputs` or `all_zero`), the reference hash and any component it substituted. These fields
feed the [stage-coverage rule](metrics.md#7-change-scope-and-stage-coverage).

## The checks

| ID | What | Runs on | Where | Oracle |
| --- | --- | --- | --- | --- |
| C0 | Structure and legality | Every output of every case and seed | Coordinator (`metrics/`) | Pure Python |
| C1 | Exact operator equivalence | Correctness suite, 1–6 qubits | Verifier (`small_exact.py`) | Dense `Operator` |
| C2 | Layout, ancillas, measurements | Correctness suite on wider targets | Verifier (`layout_semantics.py`) | Statevectors |
| C3 | Dynamic circuits | Correctness suite with control flow | Verifier (`dynamic.py`) | Exact branching simulator |
| C4 | Symbolic parameters | Parameterized fixtures | Coordinator + C1 on bound circuits | — |
| C5 | Scheduling validity | Scheduled fixtures | Verifier (`schedule.py`) | Pure Python timeline check |
| API | Pass-manager contracts | Level-2 fixtures | Worker `api_checks` mode, judged by the coordinator | — |
| C6 | Routing replay | Every scored and basis-guard seed; 10 seeds of other static guards | Coordinator (`metrics/replay.py`) | Pure Python, exact |
| C7 | Clifford variants at full scale | 10 seeds of the 100-qubit circuits' Clifford variants | Verifier (`small_exact.py`) | Stabilizer tableaux |
| C1-lite | Zero-input state equivalence of scored outputs | Confirm profile: scored cases ≤ 25 qubits, first 10 seeds | Verifier (`layout_semantics.py`) | Statevectors / measurement branches |

### C0: structure (every output)

Computed in the same streaming pass as `D2`/`N2`:

- Every instruction exists in the target (or, for a `loose` case, in the implied target:
  listed gates everywhere, two-qubit gates on each coupling-map edge).
- The **ordered** qubit tuple is supported: `(a, b)` does not license `(b, a)`.
- Correct arity and parameter count, finite parameters, angle bounds and fixed angles honored.
- Output width equals target width; valid qubit and classical-bit indices; no repeated wires.
- Layout arrays are permutations, `final[i] == routing_permutation[initial[i]]`, and a requested
  initial layout is honored.
- The exported output hash matches what the worker reported.

C0 catches illegal output, not wrong output.

### C1: exact equivalence of small circuits

The frozen correctness suite (`fixtures/correctness-suite.json`) has 480 configurations × 5
seeds = 2,400 small compiles per revision: circuits of 1–6 qubits (cancellations,
non-commuting gates, three-qubit gates, frozen random unitaries, tiny rotations from 1e-9 to
1e-4), at levels 0–3, on `cx`/`cz`/`ecr` line targets of different widths, with and without
explicit layouts.

The verifier builds the expected circuit **at the full output width**: the logical circuit on
the initial-layout wires, followed by a `PermutationGate` that moves each wire to its final
position. It then requires operator equality up to global phase (`rtol = 1e-7`,
`atol = 1e-8`), and records the process infidelity as a diagnostic. These fixtures compile
with `qubits_initially_zero=False` so that all-input equivalence is a fair contract.
One- and two-qubit fixtures are compared after embedding both operators as controlled
operations. That catches a global phase that would become a relative phase under control.

The tiny-angle fixtures exist because dropping `rz(1e-4)` changes the operator by about
5e-5, far above tolerance. A candidate cannot buy depth by silently loosening an
identity-removal threshold.

### C2: layout, ancillas and measurements

Small circuits compiled onto a target wider than the input. From the all-zeros state, the
output state must equal the logical state placed on its final physical positions, with
every other qubit in `|0⟩`. Classical register names and sizes must survive. For measured
circuits, the exact joint classical distribution must match (total variation distance
< 1e-8), including scrambled qubit-to-bit assignments.

### C3: dynamic circuits

Mid-circuit measurement, reset, `if_else` and bounded loops, up to 8 qubits. A harness-owned
exact branching simulator enumerates measurement outcomes with their probabilities and
compares the joint classical distribution. It never strips measurements to compare unitaries.

### C4: symbolic parameters

The output's free parameters must be a subset of the input's. For each declared binding
(zero, boundary angles, generic values), the worker binds the output with the revision's own
`assign_parameters`, and the bound circuit goes through C1.

### C5: scheduling

For scheduled outputs: no negative start times, no overlap on a wire, every operation starts
after its predecessors end, durations match the target, alignment and granularity constraints
hold, and delays exactly fill idle gaps.

### API contracts

For level-2 fixtures, the worker runs live checks and the coordinator judges the results:

- The input circuit is unchanged after compiling.
- A reused pass manager leaks no state: compiling A, B, then A gives identical A outputs.
- Batch compilation preserves order, layouts and metadata.
- Unsupported configurations raise `TranspilerError`: ASAP/ALAP scheduling or basic/lookahead
  routing with control flow, and scheduling without durations.

### C6: routing replay (on the scored circuits themselves)

Full equivalence of a 100-qubit generic-angle circuit cannot be computed. What layout and
routing do, however, can be checked exactly. The worker compiles the same seed twice more with
truncated pipelines:

- `L'`: the pipeline stopped after `init`, which is the virtual circuit the router receives.
- `R`: the pipeline stopped after `routing`, which is the physical circuit with SWAPs.

The replay walks `R` in order while tracking which virtual qubit sits on each physical qubit.
Each operation must either be the next pending operation of `L'` on exactly those virtual
qubits, or a SWAP that updates the mapping. At the end, every queue must be empty and the
recorded final layout must match the tracked mapping, including any permutation that `init`
elided. The full pipeline's layout must also equal the prefix's. At level 3 a difference is
recorded and skipped, because optimization may legitimately re-apply a layout.

A router that rewrites gates gives `unverified`. SWAPs that do not account for the final layout
give `mismatch`. C6 covers the **layout** and **routing** stages only. It roughly doubles the
compile cost of a quality run.

### C7: Clifford variants at scale

Each 100-qubit scored circuit has a frozen **Clifford variant**: every rotation angle is
replaced by a random odd multiple of π/2, which keeps the routing problem. Clifford circuits
can be compared exactly at any width through their stabilizer tableaux. Each revision compiles
the variant on 10 seeds in two ways:

- **Full pipeline:** covers all stages when it verifies. At levels 2–3, two-qubit
  resynthesis usually emits non-Clifford angles, so the result is often `unverified`.
- **Prefix:** optimization stage dropped, with `unitary_synthesis_method=clifford`. Covers
  init, layout, routing and translation, with unitary synthesis recorded as **substituted**.
  This is the variant `IA1/C7` requires.

### C1-lite (confirm profile)

This checks what the scored compilation actually promises: correct behavior when every
qubit starts in `|0⟩`. Eligible cases are scored cases with at most 25 logical qubits and no free
parameters, on the first 10 seeds. The verifier simulates the output and its laid-out reference
on the union of wires that either circuit touches (never dropping a logical or ancilla wire):

- Unmeasured circuits: equal statevectors up to global phase.
- Terminally measured circuits: equal joint classical distributions (TVD < 1e-8) and equal
  conditional residual states (joint trace distance < 1e-8), with an independent phase per
  outcome.
- More than 25 wires, or a statevector budget above 1 GiB, gives `unverified`.

Trotter inputs are compared against a frozen product-formula reference circuit, not the exact
exponential. C1-lite covers **all stages** for the `all_zero` input domain.

## Upstream tests

Not a verifier function, but also part of correctness (`coordinator/upstream.py`):

- The **baseline snapshot's** `test/python/transpiler` and `test/python/compiler` run against
  each build. The candidate cannot pass by weakening its own tests. The binding result is
  "no new failures compared with the baseline".
- `cargo test --locked -p qiskit-transpiler` runs in each build's source copy.
- The candidate's own Python tests run too, report-only (`upstream-evolved-own.json`).
- Budgets: 4 h for Python and 3 h for Rust (`policy.json`). A timeout gives `unresolved`.
- Changed test files are listed in `changed-tests.json`.

## Determinism audit

Quality observations must be reproducible. After the B0 block, at least 5% (minimum 10) of
observations per revision are recompiled, half of them with `PYTHONHASHSEED=1`. Each must
reproduce the same output hash and layout. If one does not, `audit/determinism` is `unresolved`,
which makes every quality verdict `unresolved`, and that revision's quality cache is deleted.
