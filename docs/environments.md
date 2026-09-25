# Environments: what is built, where, and how long it takes

A session never uses your installed Qiskit and never modifies your source folders. `compile`
copies each folder, builds each copy into its own isolated virtual environment, and installs a
pinned, trusted Qiskit separately for the verifier. The baseline build lives in the store,
where later sessions reuse it; the evolved build lives in the session. This page explains
those environments, the store, disk use and running stages on several hosts.

Code: `src/qtb/envbuild/__init__.py` (snapshot and build), `Comparison._build`,
`Comparison.baseline_build` and `Comparison.build_verifier` in
`src/qtb/coordinator/__init__.py`, and `src/qtb/coordinator/store.py` (the store).

## Four kinds of environment

| Environment | Where | Contains | Used for |
| --- | --- | --- | --- |
| Your development env | `.venv/` (from `uv sync --all-extras`) | The harness, `jsonschema`, and Qiskit 2.5.2 with pytest and ruff for development | Running the CLI (coordinator) and the harness's own tests. The coordinator never imports Qiskit |
| Baseline env | `STORE/builds/<key>/env/` | Qiskit built from the baseline snapshot, pinned runtime and test dependencies, the harness wheel | Workers: compiling, timing, memory, upstream tests. Shared by every session with the same key, and never modified after it is ready |
| Evolved env | `RESULTS_ROOT/builds/evolved-build/env/` | The same, built from the evolved snapshot | Workers, for this session only |
| Verifier env | `RESULTS_ROOT/verifier/env/` | Released Qiskit 2.5.2, pinned dependencies, the harness wheel | Semantic oracles ([verifier.md](verifier.md)) |

`compile` prepares the last three: it reuses or builds the baseline, builds the evolved
tree and creates the verifier.

## The `envs/` directory

The lock files pin everything except the Qiskit revision itself, so both revisions compile
against identical dependencies:

| File | Installed into | Pins |
| --- | --- | --- |
| `build-constraints.txt` | Revision envs | Build tooling: `pip`, `setuptools`, `setuptools-rust`, `wheel`, `semantic-version`. Qiskit declares only lower bounds for these |
| `common.lock` | Revision envs and verifier | Runtime dependencies: `numpy`, `scipy`, `rustworkx`, `dill`, `stevedore`, `sympy`, `jsonschema`, and others |
| `dev-tests.lock` | Revision envs, at build time | Test-only dependencies for the upstream test run (`pytest`, `hypothesis`, `ddt`, `testtools`, ...). `unit-tests` never installs anything itself |
| `verifier.lock` | Verifier | `qiskit==2.5.2` |

These files are bundled into the harness wheel (`qtb/data/envs`), and their hashes are part of
every build's identity.

## How a revision is built

Steps 1–4 run once; steps 5–8 run for both revisions at once, each in its own directory:

1. **Snapshot.** If the folder is a Git repository root, the file list is
   `git ls-files -co --exclude-standard` (tracked plus untracked, non-ignored files).
   Otherwise the harness walks the folder and skips `.git`, `.venv`, `build`, `dist`, `target`,
   caches and similar top-level directories. The files are copied to
   `builds/<revision>/` in the session, and each file's SHA-256 is recorded in
   `builds/<revision>.snapshot.json` with the commit ID and dirty state. The tree hash
   identifies the source. Snapshotting first means edits you make after `compile` cannot
   change what the session measures.
2. **Changed paths.** The two snapshots are compared file by file to compute the
   [change scope](metrics.md#7-change-scope-and-stage-coverage). No Git history is needed.
3. **Harness wheel.** `uv build --wheel` of this repository into `harness-wheel/`. Its hash
   enters every store key.
4. **Toolchain.** The Rust channel is read from the **baseline's** `rust-toolchain.toml` and
   forced for every build through `RUSTUP_TOOLCHAIN`. `rustc -Vv`, `cc --version` and
   `c++ --version` are recorded.
5. **Build identity and store lookup.** Both identities are computed before any build work
   ([below](#build-identity-and-keys)). For the baseline, `compile` looks up
   `STORE/builds/<key>/READY`. On a hit it checks that `build.json` and its source tree point
   inside that entry and that the native extension is unchanged, logs "Reusing baseline build
   <key> from the store", and skips steps 6–8 for the baseline. On a miss it takes
   `STORE/builds/<key>.lock`, checks again, removes an incomplete directory left by an
   interrupted build, and builds directly at `STORE/builds/<key>/`. A virtual environment holds
   absolute paths, so a build is never moved after it is made.
6. **Build.** In `builds/evolved-build/` (evolved) or `STORE/builds/<key>/` (baseline):
   - copy the snapshot to `source/`
   - `python -m venv env` (same Python as the coordinator)
   - `pip install -r common.lock -r build-constraints.txt -r dev-tests.lock`
   - `pip wheel --no-deps --no-build-isolation source/` → `wheels/qiskit-*.whl`
     (a **release** build of the Rust extension). Cargo downloads the crates into the build's
     own `cargo/` directory (`CARGO_HOME`). For the baseline, a wheel with the same identity in
     `STORE/wheels/<identity>/` is copied instead, and the Rust compile is skipped
   - `pip install --no-deps <qiskit wheel> <harness wheel>`, then `pip check`
7. **Verify provenance.** `Cargo.lock` must be unchanged by the build and exactly one wheel
   must be produced. A test import must load `qiskit` and `qiskit._accelerate` from inside
   `env/`. The native extension's SHA-256 is recorded, and every worker re-checks it before
   running a job.
8. **Record.** `build.json` stores the build identity, wheel hash, import provenance and
   full `pip freeze`. `build.log` holds the output of every command. Then
   `source/target/release` is deleted: the wheel is installed in `env/`, and `cargo test` uses
   the debug profile. A baseline build gets its `READY` marker last, after this.

The build environment is sanitized. Only `PATH`, `HOME`, temp-directory, TLS-certificate and
locale variables are inherited, so `RUSTFLAGS`, `CARGO_*`, `PYTHONPATH` and user pip config are
dropped. On top of that the harness sets `QISKIT_BUILD_PROFILE=release`,
`QISKIT_BUILD_WITH_MIMALLOC=1`, `CARGO_PROFILE_RELEASE_LTO=thin`,
`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16`, a private `CARGO_HOME` per build, and
`PIP_CONFIG_FILE=/dev/null`.

The two `CARGO_PROFILE_RELEASE_*` variables override Qiskit's own release profile (fat LTO, one
codegen unit), whose final compile and link runs on a single core. Thin LTO with 16 codegen
units compiles several times faster. Both revisions get the same profile, so the comparison
stays fair, but the Rust code is somewhat slower than in a released Qiskit wheel, and absolute
timings should not be compared with one. The variables are part of the build identity. There
is no shared Cargo registry: each build keeps its crates in its own `cargo/`, so no lock is
shared across sessions, and the evolved build downloads its crates once per session.
The Rust wheel command has a four-hour timeout; the other build commands have one hour.

The verifier env is simpler: `python -m venv`, then
`pip install -r common.lock -r verifier.lock <harness wheel>`. It is marked done with
`verifier/ready.json`.

### Build identity and keys

A build's identity is the hash of: snapshot tree hash, Python version, lock-file hashes,
`rustc -Vv`, C/C++ compiler versions, build flags, OS and CPU architecture. The build ID
(`build.json` → `id`) is the digest of the identity.

The store key of a baseline build also covers the harness wheel hash, because the harness is
installed in the venv. Baseline wheels are kept in `STORE/wheels/<identity>/`, without the
harness. A harness change therefore gives a new build key but the same wheel: it costs a new
venv and pip install, not a Rust compile.

The evolved tree has no wheel cache: every session compiles it once. Within a session, a
rerun of an unfinished `compile` keeps a finished evolved build. When both folders hold the
same source (an A/A check), the evolved build is still compiled independently and never
reuses the baseline's wheel.

## The baseline store

The store holds baseline data only: baseline builds and the baseline results of quality,
correctness and unit tests. Many sessions, on several hosts, can use one store at once.
Nothing about the evolved tree is written to it: not the evolved build, the verifier, the
verifier cache, evolved observations or any cost sample. Cost samples are never stored,
because both arms must be measured in one session.

`compile` takes the store from `--store` or, when the flag is absent, `QTB_STORE`. The flag
wins. There is no default, and the directory must already exist: `compile` creates the
subdirectories but not the root, so a mistyped path exits 64 instead of creating a stray
store. The resolved absolute path is recorded in `run.json:store`, and every later stage uses
that store. Layout: [output-format.md](output-format.md#the-store).

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

## How long it takes

Build times were measured on an earlier recorded run (Apple M1 Max, 10 cores, macOS,
Rust 1.89, Qiskit 2.6.0.dev0 source). That run used Qiskit's fat-LTO profile and built the
revisions one after another; neither applies any more, and the thin-LTO concurrent build has
not been timed yet, so expect considerably less than this:

| Step | Time |
| --- | --- |
| Snapshot of one Qiskit source tree | Seconds |
| Baseline build (venv + dependencies + release Rust compile + install) | **24 min** |
| Evolved build | **52 min** |
| Verifier env | ~45 s |

Nearly all of each build is the release compile of Qiskit's Rust extension (`qiskit._accelerate`).
Creating the venv and installing locked dependencies takes seconds when pip's cache is warm.
The two builds of the same source took very different times, so treat the numbers as a range:
roughly **20–60 minutes per Qiskit build** on a laptop, depending on load. Each build compiles
from scratch in its own target directory and downloads its own crates.

What this means for your sessions:

| Situation | Builds compiled from scratch |
| --- | --- |
| First session against a store | 2 (baseline into the store, evolved in the session) |
| New session, same baseline content, harness, lock files, toolchain and Python | 1 (evolved); the baseline build is reused with no work |
| New session after a harness edit | 1 (evolved); the baseline gets a new venv from the stored wheel, without a Rust compile |
| `compile` run again on its unfinished session | None for a finished evolved build; a half-finished evolved build directory is renamed `*.failed-<id>` and rebuilt. A baseline build without `READY` is rebuilt under its key lock |

A build command that exceeds its timeout fails `compile` (exit 40) with the path to
`build.log`, and `decide` reports `ERROR`.

## Disk use and cleanup

Measured on a confirm A/A run on 2026-09-24 (8.3 GB in total, before the automatic cleanups
below existed):

| Item | Size | Needed later? |
| --- | ---: | --- |
| `source/target/debug`, from `cargo test` | 1.9 GB per build | No, once the unit tests are done |
| `source/target/release`, from the wheel build | 1.0 GB per build | No. The wheel is installed in `env/`, and `cargo test` uses the debug profile |
| `env/` | 312 MB per build | Yes, while stages still run |
| `upstream-*/tests.jsonl` | 308 MB each (3 per session) | Only the failures |
| `verifier/` | 291 MB per session | Yes, while stages still run |

With the automatic cleanups, a build takes about 330 MB, in the store for the baseline and in
the session for the evolved tree.

**Automatic, by the stage that made it:**

| Cleanup | Done by | When |
| --- | --- | --- |
| `source/target/release` | `compile` | Right after the wheel is installed and verified, for both builds. A store build gets its `READY` marker only after this |
| Rust test build | `unit-tests` | `CARGO_TARGET_DIR` is `upstream-<revision>/target`; it is deleted when the suite ends |
| `tests.jsonl` | `unit-tests` | Gzipped to `tests.jsonl.gz`, with failure text kept only for failed tests |

**Explicit: `clean --results-root DIR`.** After `decide`, it deletes the session's bulk: the
evolved build, the snapshot copies, `verifier/`, `verifier-cache/`, worker scratch
directories, `oracle-jobs/`, the copied upstream test trees, the `upstream-*/cargo` crate
copies, and verified outputs larger than
the policy's `output_retention_bytes`. It keeps the results, evidence, verdict and logs, and
never touches the store. `decide` still works afterwards; every other stage exits 41. The
details and `clean.json` are in [output-format.md](output-format.md#cleanjson). Session
directories are otherwise never deleted automatically; to remove one, delete it.

Caches from earlier versions (`results/build-cache/`, `results/quality-cache/`,
`results/verifier-cache/`, `results/runs/`) are not migrated. A `results/` directory that
still holds them is not a session, so `compile` refuses it as a results root (exit 64). Move
or delete it, or pass another `--results-root`.

## Running stages on several hosts

Each stage can run on a different host, for example as separate LSF jobs. Then:

- **Shared filesystem, same path.** The session directory and the store must be on a shared
  filesystem mounted at the same path on every node. Virtual environments and job files hold
  absolute paths, and `run.json:store` is absolute.
- **The Python interpreter.** Build environments link to the Python that ran `compile`,
  usually a `uv` Python under `~/.local/share/uv/python`. That directory must be shared, or
  exist at the same path on every node, including the node that first built a baseline into
  the store.
- **Host check.** Before running, each stage after `compile` checks that the OS, CPU
  architecture and Python version match each build's identity, and that the build's
  interpreter exists and runs. Otherwise it exits 41.
- **Workers.** A stage uses `LSB_DJOB_NUMPROC` workers (the slots LSF granted) when it is
  set, otherwise one less than the CPU count, at most 12. There is no command-line override.
  The number is recorded in the stage's `state.json`. Quality compiles still run at most nine
  batches at once, and `cost` is serial.
- **Runner lock.** Builds, worker jobs and verifier batches hold a per-user machine lock
  shared, and `cost` holds it exclusively, then waits for the load average to fall. The lock is
  `/tmp/qtb-runner-<uid>.lock`, a fixed path rather than `TMPDIR`, which LSF often sets per
  job. `QTB_RUNNER_LOCK` overrides the path. On LSF, `bsub -x` gives `cost` its exclusive
  host; the lock and the load-average wait are a second line of defence.
- **Machine identity.** Each stage records its own host in `state.json`. `run.json:machine`
  is the `compile` host, for information only. The baseline quality key uses the `quality`
  host's CPU, and cost bundles record the `cost` host.

### LSF

`tools/lsf/submit.sh` is an example, not harness code; adapt its queues, slot counts and
walltimes to your site:

```bash
tools/lsf/submit.sh NAME BASELINE EVOLVED COST_MODEL [PROFILE] [--no-unit-tests]
```

It uses `QTB_STORE` (default `/shared/qtb-store`) and creates the session
`$QTB_SESSIONS/NAME` (default `/shared/qtb-sessions/NAME`), which must not exist yet. LSF
output goes to `$QTB_SESSIONS/lsf/`. It submits the whole chain at once:

| Job | Runs | Slots | Waits for |
| --- | --- | ---: | --- |
| `b-NAME` | `compile` | 16 | — |
| `q-NAME` | `quality` | 12 | `done(b-NAME)` |
| `c-NAME` | `correctness` | 12 | `done(q-NAME)` |
| `u-NAME` | `unit-tests` (optional) | 8 | `done(q-NAME)` |
| `k-NAME` | `tools/lsf/cost_if_gated.sh` | 1 | `done(c-NAME)` |
| `d-NAME` | `decide` | 1 | `ended(...)` of every job above |

- **`-ti`** is set on every job with a `done()` dependency. It ends a job whose dependency can
  never be met, for example after `compile` failed. Without it the job would stay pending
  forever, and `decide`, which waits on `ended()`, would never start.
- **`cost_if_gated.sh SESSION COST_MODEL`** runs as a light one-slot job. When the gate is
  closed, or correctness found a failure, it runs `cost` locally, which records `skipped`
  within seconds. Otherwise it submits `cost` with `bsub -K -x -R "select[model==COST_MODEL]"`
  and exits with that job's status. **`-x`** gives `cost` an exclusive host, **`-K`** waits
  for the job to end (and also fails when the submission fails), and
  **`select[model==...]`** pins cost to one host model, because the fixed cost thresholds
  were set on one machine type.
- **Without unit tests,** pass `--no-unit-tests`: the `u-` job is not submitted and `decide`
  does not wait for it.

A gated stage whose gate is closed starts, records `skipped` and exits 0 within seconds, so
the `done()` dependencies after it still hold.

## Runtime environment of a worker

Workers and the verifier run with a sanitized environment plus a fixed serial configuration,
so every revision compiles under identical conditions:

| Variable | Value | Why |
| --- | --- | --- |
| `QISKIT_PARALLEL` | `FALSE` | No multiprocess dispatch |
| `QISKIT_IGNORE_USER_SETTINGS` | `TRUE` | A user config file can change the default level, seed or SABRE budget |
| `RAYON_NUM_THREADS`, `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` | `1` | Serial Rust and BLAS work |
| `PYTHONHASHSEED` | `0` | Pinned; the determinism audit deliberately uses `1` for half its samples |
| `PYTHONNOUSERSITE`, `PYTHONSAFEPATH` | `1` | No user site-packages, and the working directory is kept off `sys.path` |
| `PYTHONDONTWRITEBYTECODE` | `1` | Workers only: a stored baseline build is shared and never written to |

Processes run with `python -P` from a scratch directory, so a source tree can never shadow the
installed package.

## Requirements

- Linux or macOS
- Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and Git
- `rustup` with the baseline's toolchain channel available (`rustup toolchain install <channel>`,
  with the channel taken from the baseline's `rust-toolchain.toml`)
- A C/C++ toolchain (`cc` and `c++` on `PATH`)
- Network access, or a warm pip cache, for pinned dependencies; network access for crates,
  which each build downloads into its own `cargo/`
- An existing store directory, given with `--store` or `QTB_STORE`
- Each source folder must be a Qiskit checkout with `rust-toolchain.toml` and `Cargo.lock`.
  Git submodules must be materialized as files
- For several hosts: the shared-filesystem and interpreter requirements
  [above](#running-stages-on-several-hosts)

## Troubleshooting a build

- Read `builds/evolved-build/build.log` in the session, or `STORE/builds/<key>/build.log` for
  the baseline. Each command is prefixed with `COMMAND [...]`.
- "Cannot resolve baseline toolchain": run `rustup toolchain install <channel>` for the channel
  in the baseline's `rust-toolchain.toml`.
- "Qiskit build modified Cargo.lock": the evolved tree's `Cargo.lock` does not match its
  `Cargo.toml` files. Update and commit the lock file in the source folder.
- "Import provenance escaped isolated environment": something on the path shadows the
  installed Qiskit. Check for a stray `PYTHONPATH` or `.pth` file.
- "This host cannot run the baseline build" (exit 41): the stage runs on a host with another
  OS, architecture or Python, or the build's interpreter is not at the same path there.
- `compile` catches all of these build problems before any quality, correctness or cost work.
