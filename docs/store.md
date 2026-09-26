# The baseline store

[Documentation index](README.md) · [compile](workflow/compile.md) · [Sessions](sessions.md)

## Reuse and keys

The store holds baseline data only: baseline builds and the baseline results of quality,
correctness and unit tests. Many sessions, on several hosts, can use one store at once.
Nothing about the evolved tree is written to it: not the evolved build, the verifier, the
verifier cache, evolved observations or any cost sample. Cost samples are never stored,
because both arms must be measured in one session.

`compile` takes the store from `--store` or, when the flag is absent, `QTB_STORE`. The flag
wins. There is no default, and the directory must already exist: `compile` creates the
subdirectories but not the root, so a mistyped path exits 64 instead of creating a stray
store. The resolved absolute path is recorded in `run.json:store`, and every later stage
uses that store. See [layout](#layout) below.

| Entry | Key covers | Written when |
| --- | --- | --- |
| `builds/<key>/` | Build identity and the harness wheel hash | `compile` built it and it passed verification |
| `wheels/<identity>/` | Build identity | The Rust compile of a baseline finished |
| `quality/<key>/` | Build ID, case definition, CPU model of the `quality` host, worker protocol, harness, quality measurement protocol, seed block and mode, worker environment | Per seed. A failed determinism audit writes `invalidated.json` into the matching entries, which are then never read |
| `correctness/<key>/` | Build ID, implementation, harness, correctness-suite file, manifest and policy hashes, verifier identity, CPU model of the `correctness` host, worker environment, Clifford seed count and mode, tolerances | Every baseline check (C1–C5, API contracts, C7) is decisive (`verified` or `mismatch`) and no worker failed or timed out |
| `unit-tests/<key>/` | Build ID, harness, `dev-tests.lock`, the baseline test tree (`test/` files of the snapshot), test budgets, CPU model, worker environment | Both baseline suites completed |

On a hit, the stage copies the stored rows and records into the session, marks each with
`cached_from: <key>`, and runs only the evolved half. An A/A session (identical build IDs)
reuses the baseline build but never the stored baseline results, so it stays an independent
check.

The baseline is reused only when all of these hold. Otherwise that part is rebuilt or
recomputed:

- **Same store.** Pass the same `--store`, or set the same `QTB_STORE`.
- **Same baseline content.** The tree hash of the snapshot counts, not the folder path. A new
  commit or any edited file in the baseline folder is a new baseline.
- **Same harness version, lock files, Rust toolchain and Python.** The harness wheel hash is
  in every key, so any edit to the harness gives a new baseline venv (not a new Rust compile)
  and recomputed baseline quality, correctness and unit-test results.
- **Same CPU model** for the baseline quality, correctness and unit-test results. The build
  itself depends only on the OS and architecture.

A ready build entry is immutable. `unit-tests` runs `cargo test` for a stored build with a
session-local `CARGO_HOME` (`upstream-baseline/cargo`) and `CARGO_TARGET_DIR`, and never
installs into the stored `env/`. Workers and the upstream pytest run set
`PYTHONDONTWRITEBYTECODE=1`, so no bytecode is written into a stored build.

### Maintaining the store

There is no command for it; maintain it by hand.

- **Size.** `du -sh $STORE/*` shows it. A baseline build takes about 330 MB, plus a few MB of
  results per baseline.
- **Deleting.** Delete an entry, or the whole store, when no session is running against it.
  Every entry can be recomputed, so nothing is lost but time. A session whose baseline build
  was deleted cannot run more stages (exit 41, naming the key), but its `decide` still works,
  because the evidence is in the session.
- **Leftovers.** A `builds/<key>/` directory without `READY` is an interrupted build. The
  next `compile` that needs it repairs it under `<key>.lock`; you can remove it when that lock
  is not held and no session is using it.

## Layout

```text
STORE/
  builds/<key>/          READY, build.json, env/, source/, cargo/, wheels/, build.log
  builds/<key>.lock      held while that key is being built
  wheels/<identity>/     baseline Qiskit wheels
  quality/<key>/         baseline quality observations with their output and job files;
                         invalidated.json after a failed determinism audit
  correctness/<key>/     rows.jsonl, evidence.json, provenance.json
  unit-tests/<key>/      python.json, rust.json, their logs, provenance.json
```

A build entry without `READY` is an interrupted build and is never read. A correctness or
unit-test entry is published in one rename; its `provenance.json` records the key, the
session that computed it and `first_computed`. Result paths in stored rows point to files
owned by this entry; they never depend on the session that populated it. Baseline
correctness entries contain baseline rows from `correctness.jsonl` and `clifford.jsonl` plus
the baseline behavior, API and C7 records, `*1/C1-C5/baseline` and `baseline/preflight`.
Combined aggregates are recomputed in each session from the stored baseline half and fresh
evolved half.
