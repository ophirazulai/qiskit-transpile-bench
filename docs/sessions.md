# Sessions and stage rules

[Documentation index](README.md) · [Start with compile](workflow/compile.md)

A session is one baseline/evolved experiment. A store holds reusable baseline work across
many sessions. See [baseline store](store.md) for its keys and maintenance.

## One session per results root

`--results-root` *is* the session directory. It holds one baseline/evolved pair: the
snapshots, the evolved build, the verifier, every stage's output and the verdict. There is
no `runs/` level inside it; use a new directory for each pair.

- **`compile` creates the session.** It refuses a directory that is already a session for
  other sources, another store or another profile, and a directory that exists but is not a
  session (exit 64). Run again with the same arguments, it resumes an unfinished `compile`.
- **The later stages take only `--results-root`.** They read the profile and the store from
  the session's `run.json`, so every stage of a session uses the same store.
- **A finished stage never runs again.** A `complete` or `skipped` stage prints
  `already complete` (or `already skipped`) and exits 0, so a whole chain can be resubmitted
  safely. A stage that failed, was killed or ended `noisy` resumes where it stopped when you
  run it again.
- **To measure again, start a new session.** With the store, a new session pays only for
  the evolved build and the evolved half of each stage.
- **The harness must not change during a session.** A stage refuses to run (exit 41) when the
  harness code or profile differs from what `compile` recorded; the message names the
  archived harness wheel. `decide` uses the session's archived profile and still works with
  a newer harness.
- **After `compile`, edits to the source folders do not reach the session.** Builds come from
  the snapshots.

## Locks, prerequisites and retries

1. **Locks.** Take the session's `lifecycle.lock` shared, then `stages/<stage>/lock`
   exclusive. Both are non-blocking: if either is held, exit 41.
2. **Refuse a cleaned session**, or one whose `clean` was interrupted (exit 41).
3. **Never rerun a finished stage.** A `complete` or `skipped` stage prints
   `already complete` (or `already skipped`) and exits 0.
4. **Check the harness.** Load `run.json` and compare the coordinator, implementation,
   manifest and policy hashes with the current harness. A change since `compile` exits 41,
   and the message names the archived harness wheel.
5. **Check prerequisites.** `quality` needs `compile`; `correctness` and `unit-tests` need
   `quality`; `cost` needs `correctness`. Each must be `complete` or `skipped`, otherwise exit
   41 with the command to run.
6. **Check the store.** The store in `run.json:store` and the baseline build entry must exist.
7. **Check the gate** (gated stages only). If the stage should not run, write `skipped` with
   the reason and exit 0.
8. **Check the host.** The OS, architecture and Python version must match the build identity,
   and the build's Python must exist and run. Otherwise exit 41.
9. **Check the cost measurement** (`cost` only). A session whose `run.json` names a
   `cost_evidence` requirement is measured only under that measurement extension (an LSF
   session: only through `lsf.job`). Otherwise exit 41. The extension then checks its
   allocation (exit 41, nothing written) and probes the idle measurement core.
10. **Write `running`, do the work, commit the evidence, then write `complete`.** The final
    `state.json` is the commit marker. A crash writes `harness/error/<stage>` and `failed`
    (exit 40). Positively detected interference writes `noisy` and commits nothing (exit 42).
    A successful retry drops the stage's old crash record.

A stage's exit status reports whether it ran, not what it found; findings go into evidence.

A stage that is `running` (after a killed job), `failed` or `noisy` resumes when run again:

- `compile` keeps a finished evolved build;
- `quality` skips seeds already in `observations.jsonl`;
- `correctness` truncates `correctness.jsonl` and `clifford.jsonl`, resets its evidence and
  restarts; the verifier cache makes repeated checks cheap;
- `unit-tests` resets its evidence and replaces its results;
- `cost` resets its evidence and reuses complete, admissible regime bundles; a regime that
  was interrupted is measured again from scratch.

`noisy` is unfinished, like `running`: nothing measured by that invocation is committed,
`decide` treats the stage as pending (normally `INCONCLUSIVE`) and `clean` refuses the
session. The harness never decides whether to run the stage again; on LSF the manager does,
within its retry budget ([LSF session guide](cluster.md#7-what-happens-inside)).

Every state records the identity of the invocation that wrote it (`invocation`: on LSF the
job key, attempt and job ID), and every invocation's outcome is appended to
`stages/<stage>/invocations.jsonl`.

To measure a finished stage again, start a new session.

## Exit codes of the stages

A stage's exit status says whether it ran, not what it found. A correctness mismatch or a
cost breach is a finding: it goes into the evidence, the stage is complete, and the exit
status is 0. Only `decide` turns findings into a verdict and exits with the verdict's code
([verdict codes](workflow/decide.md#reading-the-verdict)).

| Exit | Meaning |
| ---: | --- |
| 0 | The stage is complete or skipped (also when it already was) |
| 40 | The stage failed: a harness, build or worker error. Its state is `failed`, and `decide` gives `ERROR` |
| 41 | A precondition is not met: an earlier stage is missing or unfinished, the directory is not a session, the session is cleaned or being cleaned, the harness or profile changed since `compile`, the store or the baseline build is missing, this host cannot run the builds, the same stage is already running, or (for `cost`) the session requires monitored evidence that this invocation cannot produce |
| 42 | `cost` detected interference on its measurement core. Its state is `noisy`; run it again (on LSF the manager does, in a new job) |
| 64 | Usage error: bad options, no store given, the store does not exist, or (for `compile`) the results root is another session's or not a session |

`decide` on a directory that is not a session exits 41; otherwise it exits with the verdict
and then cleans the session unless a safety check refuses ([clean](workflow/clean.md)).
`clean` exits 0 or 41.

## Workers

The harness never reads a scheduler's environment. A scheduler entry point translates its
allocation into an execution context (`qtb.execution`): on LSF, `lsf/context.py` turns the
granted slots into the worker count and the job's identity into the state's `scheduler`
record. Without one the default is one less than the CPU count, at most twelve and at least
one. There is no command-line override. Quality has an additional limit of nine concurrent
batches, `compile` splits the granted slots between its two concurrent builds, and cost
measurements are serial. Each stage records the count and its host in `state.json`.

## Reproducibility

- Every quality panel uses seeds 0–99 (block B0).
- Workers run serially (`QISKIT_PARALLEL=FALSE`, one Rust and BLAS thread, `PYTHONHASHSEED=0`),
  and a determinism audit recompiles a sample to prove outputs are reproducible.
- Baseline quality results are stored by build, case definition, CPU model, harness and
  measurement protocol, so a new session against the same baseline only compiles the
  candidate. A failed determinism audit invalidates the stored baseline quality results it
  covers.
- Cost samples belong to one session and are never stored or reused: both arms are measured
  again in every session.
- Profiles and fixtures are frozen and hashed. No comparison regenerates its inputs.
  Six inputs whose redistribution terms were unclear were replaced by recorded Qiskit
  constructions; see [fixture provenance](../fixtures/PROVENANCE.md).
- A changed fixture, weight, threshold or canary value requires a new profile version. See
  [versioning](versioning.md).

## Older results directories

Caches from earlier versions (`results/build-cache/`, `results/quality-cache/`,
`results/verifier-cache/`, `results/runs/`) are not migrated. A `results/` directory that
still holds them is not a session, so `compile` refuses it as a results root (exit 64). Move
or delete it, or pass another `--results-root`.
