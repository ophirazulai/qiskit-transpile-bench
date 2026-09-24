# qiskit-transpile-bench

Compare two Qiskit source trees against frozen transpilation workloads. The objective is
native two-qubit depth; gate count, compilation cost, and correctness are constraints.
The coordinator and evaluator do not import Qiskit. Revision workers compile and export
circuits; independent code computes metrics, legality, routing replay, and verdicts.
Semantic oracles run in a separate Qiskit 2.5.2 environment.

```bash
uv sync --all-extras
uv run qiskit-transpile-bench smoke --baseline /path/to/baseline --evolved /path/to/evolved
uv run qiskit-transpile-bench compare --baseline /path/to/baseline --evolved /path/to/evolved
uv run qiskit-transpile-bench compare --baseline /path/to/baseline --evolved /path/to/evolved --profile confirm-profile
```

Linux or macOS, Python 3.11+, Git, uv, rustup, a C/C++ toolchain, and access to the pinned Python/Rust
dependencies are required for source builds. Original source folders are never modified.
Builds use independent virtual environments and release wheels, the baseline's Rust
channel, locked dependencies, sanitized environment variables, and native-extension hashes.

**Qualification remains required.** This repository ships executable profiles and harness
validation, not a claim that a controlled runner has qualified them. Automatic `PASS`
requires all correctness, baseline, calibration, coverage, and qualification records.
Missing or unverified evidence remains `INCONCLUSIVE`. In particular, the baseline's
output-pinned upstream-test exclusions need the reviewed derivation described in the plan.
See [qualification and implementation status](docs/implementation-status.md).

The iterations profile contains three scored 100-qubit cases, six basis guards, three
canary cases, 19 timing cases, and three preset cases. Confirm has 133 scored cases in
38 input groups, eight families, 14 workload targets, 31 additional timing cases, and nine
memory cases. Six inputs with unresolved upstream redistribution terms were replaced by
explicitly recorded Qiskit synthesis constructions, as allowed by the design plan; see
[fixture provenance](fixtures/PROVENANCE.md). No comparison regenerates its inputs.

Every quality panel uses seeds 0–99. Calibration uses the separate baseline blocks
100–199 and 200–299. Quality caches include the build, per-case definition, measurement
protocol, machine, harness, and worker environment. Cost samples belong to a single
run/session/arm bundle and are never reused for a different comparison.

```bash
uv run qiskit-transpile-bench calibrate --baseline /path/to/baseline
uv run qiskit-transpile-bench derive-exclusions results/runs/RUN
uv run qiskit-transpile-bench evaluate results/runs/RUN
uv run qiskit-transpile-bench report results/runs/RUN
uv run qiskit-transpile-bench repro OBSERVATION_ID --run results/runs/RUN --out repro-job
uv run qiskit-transpile-bench review --decision results/runs/RUN/decision.json \
  --reviewer 'Reviewer Name' --rationale rationale.md --outcome reviewed_reject
```

`--results-root` selects the evidence directory. `--resume RUN_DIRECTORY` resumes an
interrupted comparison against its archived snapshots and original manifest/policy. Resume
and replay use the same coordinator version; install the archived harness wheel when
inspecting an older run.
`--change-scope scope.json` accepts `{"stages":["optimization"]}` and can only widen the
scope inferred from changed source files. Unknown paths mean all stages.

Verdict exit codes are `PASS=0`, `NO_IMPROVEMENT=10`, `CONSTRAINT_VIOLATION=20`,
`INCONCLUSIVE=30`, `ERROR=40`; usage errors are 64. Utilities return 0 or 40.
Smoke writes `smoke.json` and never creates a decision. Human review is recorded separately
and never changes a verdict to `PASS` or changes the reference revision.

```bash
uv run pytest
uv run ruff check src worker verifier tests
uv run python tools/probes/confirm_panel.py
```

The profile is the scope of the claim. Iterate with iterations-profile, then use confirm
as a check. Repeated tuning on confirm invalidates its intended role. Reports retain the
per-manifest decision count and the confirm score with the iteration inputs removed.

See [format and compatibility rules](docs/versioning.md) for profile changes and archival replay.
