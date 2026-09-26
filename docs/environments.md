# Setup and runtime environments

[Documentation index](README.md) · [compile](workflow/compile.md)

Install the harness and prepare the tools needed to build Qiskit. The coordinator runs in
its own environment and never imports either source revision. Build environments and the
trusted verifier are prepared by [compile](workflow/compile.md); source folders are copied
and never modified.

```bash
uv sync --all-extras
```

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
  [in the LSF session guide](cluster.md#shared-paths-and-compatible-hosts)

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

## Related guides

- [compile](workflow/compile.md): snapshots, release wheels, build identities, provenance,
  build timings and troubleshooting.
- [Baseline store](store.md): shared baseline builds and results, keys and maintenance.
- [LSF session guide](cluster.md): starting and watching sessions, shared paths, interpreter availability.
- [clean](workflow/clean.md): disk use and cleanup scope.
