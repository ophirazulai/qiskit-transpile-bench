# 4. unit-tests (optional)

[Documentation index](../README.md) · [← correctness](correctness.md) · [cost →](cost.md)

Run upstream Python and Rust tests on both revisions. Neither profile requires this stage,
but once started its results bind the verdict.

## Run this step

```bash
uv run qiskit-transpile-bench unit-tests --results-root "$S"
```

`S` is the session directory chosen at `compile`. `quality` must be complete and the gate
open. This stage can run alongside `correctness` on a busy host. See [session
rules](../sessions.md) for prerequisites, retries and stage exit codes.

## What runs

Implemented in `src/qtb/coordinator/upstream.py`. These suites are **optional**: they form
their own stage, `unit-tests`, which either profile may run once the quality gate is open,
alongside `correctness`. The rule is: **if you run it, it counts.** Once the stage has
started (running, failed or complete), `decide` requires `IA1/upstream` or `CA1/upstream`.
If it never started, or the gate skipped it, the verdict is decided without it and the
report says "Upstream tests: not run."

- The **baseline snapshot's** `test/python/transpiler` and `test/python/compiler` run under
  pytest against each build. The candidate cannot pass by weakening its own tests.
- pytest runs with `--rootdir` set to the test copy and `-c` pointing at the snapshot's own
  pytest configuration (an empty one when the snapshot has none, as Qiskit does). The harness
  project's settings never apply, and node IDs are identical across revisions.
- The runner script has a `__main__` guard. Without it, macOS `spawn` workers re-import it
  and re-run the whole suite, which breaks every parallel-transpile test.
- `cargo test --locked --no-fail-fast -p qiskit-transpiler` runs in each build's source copy,
  and failures are parsed test by test.
- **Stored builds stay unchanged.** The suites never install into a build environment: the
  test dependencies (`envs/dev-tests.lock`) are installed when the build is made. pytest runs
  with `PYTHONDONTWRITEBYTECODE=1`, as do the compile workers. The Rust tests of the baseline
  use a session-local `CARGO_HOME` (`upstream-baseline/cargo`); the evolved build uses its own
  `cargo/` in the session. `CARGO_TARGET_DIR` is `upstream-<rev>/target`, deleted when the
  suite ends.
- **Baseline results from the store.** When both baseline suites completed in an earlier
  session with the same build, harness, `dev-tests.lock`, baseline test tree, budgets, CPU
  model and worker environment, their results are read from the store and only the evolved
  suites run. A/A sessions always run both.
- **Known-bad baseline failures.** Tests that fail on the baseline are recorded on
  `*1/upstream/baseline` (result `unresolved`, list in `known_bad`) and never count against
  the candidate. The evolved build fails only on a **regression**: a test that fails but
  passed on the baseline, or a baseline-passing test that no longer passes (failed, skipped
  or missing, for example after a collection error). An incomplete suite gives `unresolved`.
- The candidate's own Python tests run too, report-only (`upstream-evolved-own.json`).
- Budgets: 4 h for Python and 3 h for Rust (`policy.json`). A timeout gives `unresolved`.
- Changed test files are listed in `changed-tests.json`.

Qiskit's own CI runs this suite with stestr (unittest), not pytest. Nine
`TestUnitarySynthesisPlugin` tests install their mock plugins in `setUpClass` in a way that
works under unittest but not under pytest, so they appear as known-bad baseline failures.

## Outputs and retries

The stage owns `upstream-baseline/`, `upstream-evolved/`, `upstream-evolved-own/`,
`upstream-evolved-own.json`, `changed-tests.json` and `stages/unit-tests/`. Python test
records are gzipped as `tests.jsonl.gz`, with detailed text kept only for failed tests. The
Rust target directory is deleted when the suite ends. A retry resets stage evidence and
replaces the suite results.

## Budget and next step

Measured Python runs are roughly 3–4 minutes each: baseline-owned tests on both builds and
the evolved tree's own tests. Rust runs for both builds as well. The budgets are four hours
for Python and three for Rust; a complete baseline result from the store saves its runs.

This stage does not block [cost](cost.md), which depends only on correctness. Once unit
tests have started, run [decide](decide.md) after they finish so their result counts. If you
start unit tests after an earlier decision, rerun `decide` before [clean](clean.md).
