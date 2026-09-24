# Environments: what is built, where, and how long it takes

A comparison never uses your installed Qiskit and never modifies your source folders. It copies
each folder, builds each copy into its own isolated virtual environment, and installs a pinned,
trusted Qiskit separately for the verifier. This page explains those environments.

Code: `src/qtb/envbuild/__init__.py` (snapshot and build) and `Comparison._build` /
`Comparison.build_verifier` in `src/qtb/coordinator/__init__.py`.

## Four kinds of environment

| Environment | Where | Contains | Used for |
| --- | --- | --- | --- |
| Your development env | `.venv/` (from `uv sync --all-extras`) | The harness, `jsonschema`, and Qiskit 2.5.2 with pytest and ruff for development | Running the CLI (coordinator) and the harness's own tests. The coordinator never imports Qiskit |
| Revision envs | `results/runs/<run>/builds/<revision>-build/env/` | Qiskit built from that revision's snapshot, pinned runtime dependencies, the harness wheel | Workers: compiling, timing, memory, upstream tests |
| Control env | `results/runs/<run>/builds/control-build/env/` | A second, independent build of the **baseline** | The A/A arm for cost calibration and in-run noise control. `compare` only |
| Verifier env | `results/runs/<run>/verifier/env/` | Released Qiskit 2.5.2, pinned dependencies, the harness wheel | Semantic oracles ([verifier.md](verifier.md)) |

`smoke` builds baseline, evolved and verifier. `compare` also builds the control env.

## The `envs/` directory

The lock files pin everything except the Qiskit revision itself, so both revisions compile
against identical dependencies:

| File | Installed into | Pins |
| --- | --- | --- |
| `build-constraints.txt` | Revision envs | Build tooling: `pip`, `setuptools`, `setuptools-rust`, `wheel`, `semantic-version`. Qiskit declares only lower bounds for these |
| `common.lock` | Revision envs and verifier | Runtime dependencies: `numpy`, `scipy`, `rustworkx`, `dill`, `stevedore`, `sympy`, `jsonschema`, and others |
| `dev-tests.lock` | Revision envs | Test-only dependencies for the upstream test run (`pytest`, `hypothesis`, `ddt`, `testtools`, ...) |
| `verifier.lock` | Verifier | `qiskit==2.5.2` |

These files are bundled into the harness wheel (`qtb/data/envs`), and their hashes are part of
every build's identity.

## How a revision is built

For each revision (baseline, evolved, and control for `compare`):

1. **Snapshot.** If the folder is a Git repository root, the file list is
   `git ls-files -co --exclude-standard` (tracked plus untracked, non-ignored files).
   Otherwise the harness walks the folder and skips `.git`, `.venv`, `build`, `dist`, `target`,
   caches and similar top-level directories. The files are copied to
   `builds/<revision>/`, and each file's SHA-256 is recorded in `builds/<revision>.snapshot.json`
   with the commit ID and dirty state. The tree hash identifies the source. Snapshotting first
   means edits you make during a long run cannot change what is measured.
2. **Changed paths.** The two snapshots are compared file by file to compute the
   [change scope](metrics.md#7-change-scope-and-stage-coverage). No Git history is needed.
3. **Harness wheel.** `uv build --wheel` of this repository into `harness-wheel/`. Its hash
   enters every cache key.
4. **Toolchain.** The Rust channel is read from the **baseline's** `rust-toolchain.toml` and
   forced for every build through `RUSTUP_TOOLCHAIN`. `rustc -Vv`, `cc --version` and
   `c++ --version` are recorded.
5. **Build.** In `builds/<revision>-build/`:
   - copy the snapshot to `source/`
   - `python -m venv env` (same Python as the coordinator)
   - `pip install -r common.lock -r build-constraints.txt -r dev-tests.lock`
   - `pip wheel --no-deps --no-build-isolation source/` → `wheels/qiskit-*.whl`
     (a **release** build of the Rust extension)
   - `pip install --no-deps <qiskit wheel> <harness wheel>`, then `pip check`
6. **Verify provenance.** `Cargo.lock` must be unchanged by the build and exactly one wheel
   must be produced. A test import must load `qiskit` and `qiskit._accelerate` from inside
   `env/`. The native extension's SHA-256 is recorded, and every worker re-checks it before
   running a job.
7. **Record.** `build.json` stores the build identity, wheel hash, import provenance and
   full `pip freeze`. `build.log` holds the output of every command.

The build environment is sanitized. Only `PATH`, `HOME`, temp-directory, TLS-certificate and
locale variables are inherited, so `RUSTFLAGS`, `CARGO_*`, `PYTHONPATH` and user pip config are
dropped. On top of that the harness sets `QISKIT_BUILD_PROFILE=release`,
`QISKIT_BUILD_WITH_MIMALLOC=1`, a private `CARGO_HOME` per build, and
`PIP_CONFIG_FILE=/dev/null`. Cargo registry downloads are shared under
`results/build-cache/cargo-registry/`; build configuration and compiled targets remain private.
The Rust wheel command has a four-hour timeout; the other build commands retain one hour.

The verifier env is simpler: `python -m venv`, then
`pip install -r common.lock -r verifier.lock <harness wheel>`. It is marked done with
`verifier/ready.json`.

### Build identity and the wheel cache

A build's identity is the hash of: snapshot tree hash, Python version, lock-file hashes,
`rustc -Vv`, C/C++ compiler versions, build flags, OS and CPU architecture. Built wheels are
cached in `results/build-cache/<slot>/<identity>/`, where the slot is `baseline`, `evolved`
or `control`. When a later run has the same identity in the same slot, the wheel is copied
from the cache and the Rust compile is skipped. The venv and dependency install still run.

The slots are separate on purpose. The control build must be an independent build of the
baseline, and it never reuses the baseline's wheel.

## How long it takes

Measured on the recorded smoke run (`results/smoke-validation/runs/20260924T135815-5702c324`,
Apple M1 Max, 10 cores, macOS, Rust 1.89, Qiskit 2.6.0.dev0 source):

| Step | Time | Disk |
| --- | --- | --- |
| Snapshot of one Qiskit source tree | Seconds | ~43 MB per revision |
| Baseline build (venv + dependencies + release Rust compile + install) | **24 min** | ~1.9 GB (`source/` including the Rust `target/`, `env/`, `cargo/`) |
| Evolved build | **52 min** | ~1.9 GB |
| Verifier env | ~45 s | ~290 MB |
| Smoke quality: 24 compiles with C0 checks | ~2 min | |
| Whole smoke run | ~1 h 20 min | ~4.2 GB |

Nearly all of each build is the release compile of Qiskit's Rust extension (`qiskit._accelerate`).
Creating the venv and installing locked dependencies takes seconds when pip's cache is warm.
The two builds of the same source took very different times, so treat the numbers as a range:
roughly **20–60 minutes per Qiskit build** on a laptop, depending on load. Each build compiles
from scratch in its own target directory, while later builds reuse downloaded crates.

What this means for your runs:

| Situation | Builds compiled from scratch |
| --- | --- |
| First `smoke` | 2 (baseline, evolved) |
| First `compare` | 3 (baseline, evolved, control) |
| Re-running with unchanged baseline source | Baseline and control come from the wheel cache; only a changed evolved tree recompiles |
| `--resume` of an interrupted run | None for builds whose `build.json` already exists; a half-finished build directory is renamed `*.failed-<id>` and rebuilt |

Every build command has a **1-hour timeout**. A slower machine that cannot compile Qiskit in an
hour gets `ERROR` with the path to `build.log`.

Plan for about 2 GB of disk per build and 5–7 GB per run before pruning. Run directories are not
deleted automatically. Successful outputs larger than 8 MB are pruned after a `PASS` or
`NO_IMPROVEMENT` decision, keeping their hashes in `retention.json`.

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

Processes run with `python -P` from a scratch directory, so a source tree can never shadow the
installed package.

## Requirements

- Linux or macOS
- Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and Git
- `rustup` with the baseline's toolchain channel available (`rustup toolchain install <channel>`,
  with the channel taken from the baseline's `rust-toolchain.toml`)
- A C/C++ toolchain (`cc` and `c++` on `PATH`)
- Network access or warm pip and Cargo registry caches for pinned dependencies and crates
- Each source folder must be a Qiskit checkout with `rust-toolchain.toml` and `Cargo.lock`.
  Git submodules must be materialized as files

## Troubleshooting a build

- Read `results/runs/<run>/builds/<revision>-build/build.log`. Each command is prefixed with
  `COMMAND [...]`.
- "Cannot resolve baseline toolchain": run `rustup toolchain install <channel>` for the channel
  in the baseline's `rust-toolchain.toml`.
- "Qiskit build modified Cargo.lock": the evolved tree's `Cargo.lock` does not match its
  `Cargo.toml` files. Update and commit the lock file in the source folder.
- "Import provenance escaped isolated environment": something on the path shadows the
  installed Qiskit. Check for a stray `PYTHONPATH` or `.pth` file.
- Run `smoke` first after changing toolchains or source trees. It catches all of these
  without the correctness and calibration work of `compare`.
