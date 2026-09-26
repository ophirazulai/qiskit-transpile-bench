# 1. compile

[Documentation index](../README.md) · [Setup](../environments.md) · [quality →](quality.md)

Create one session, prepare the baseline and evolved builds, create the trusted verifier,
and prove that both revisions reconstruct the same frozen inputs.

## Create the session

```bash
uv sync --all-extras
mkdir -p "$HOME/qtb-store"
export QTB_STORE="$HOME/qtb-store"
S="$HOME/qtb-sessions/idea1"  # choose a path that does not already exist

uv run qiskit-transpile-bench compile \
  --baseline /path/to/baseline \
  --evolved /path/to/evolved \
  --results-root "$S"
```

Use `--profile confirm-profile` for the confirm workload; the default is
`iterations-profile`. Choose a profile before building: later stages read it from the
session. See [profile definitions](../README.md#profiles).

| Option | Meaning |
| --- | --- |
| `--baseline PATH`, `--evolved PATH` | Required Qiskit source folders |
| `--store DIR` | Required unless `QTB_STORE` is set; the flag wins. The directory must already exist |
| `--profile NAME` | `iterations-profile` (default) or `confirm-profile` |
| `--results-root DIR` | The session directory (default `results/`); one baseline/evolved pair |

Your source folders are copied and never modified. Edits after the snapshots are made do not
reach this session. A directory that already exists but is not a session, including an empty
directory, is a usage error (64). An existing session must have the same sources, store and
profile. Repeating the command resumes an unfinished build; a finished one exits 0 without
doing more work.

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
   [change scope](../metrics.md#7-change-scope-and-stage-coverage). No Git history is needed.
3. **Harness wheel.** `uv build --wheel` of this repository into `harness-wheel/`. Its hash
   enters every store key.
4. **Toolchain.** The Rust channel is read from the **baseline's** `rust-toolchain.toml` and
   forced for every build through `RUSTUP_TOOLCHAIN`. `rustc -Vv`, `cc --version` and
   `c++ --version` are recorded.
5. **Build identity and store lookup.** Both identities are computed before any build work
   ([below](compile.md#build-identity-and-keys)). For the baseline, `compile` looks up
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
locale variables are inherited, so `RUSTFLAGS`, `CARGO_*`, `PYTHONPATH` and user pip config
are dropped. On top of that the harness sets `QISKIT_BUILD_PROFILE=release`,
`QISKIT_BUILD_WITH_MIMALLOC=1`, `CARGO_PROFILE_RELEASE_LTO=thin`,
`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16`, a private `CARGO_HOME` per build, and
`PIP_CONFIG_FILE=/dev/null`.

The two `CARGO_PROFILE_RELEASE_*` variables override Qiskit's own release profile (fat LTO,
one codegen unit), whose final compile and link runs on a single core. Thin LTO with 16
codegen units compiles several times faster. Both revisions get the same profile, so the
comparison stays fair, but the Rust code is somewhat slower than in a released Qiskit wheel,
and absolute timings should not be compared with one. The variables are part of the build
identity. There is no shared Cargo registry: each build keeps its crates in its own
`cargo/`, so no lock is shared across sessions, and the evolved build downloads its crates
once per session. The Rust wheel command has a four-hour timeout; the other build commands
have one hour.

The verifier env is simpler: `python -m venv`, then `pip install -r common.lock -r
verifier.lock <harness wheel>`. It is marked done with `verifier/ready.json`.

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

## Input round-trip

After both builds and the verifier are ready, each revision rebuilds every distinct frozen
input circuit and target, including correctness fixtures and Clifford variants. The exports
must reproduce the frozen hashes. A mismatch fails the stage with exit 40; success commits
`harness/roundtrip`. This establishes that both revisions received identical inputs.

## Outputs and reuse

`compile` owns `run.json`, the archived `manifest.json` and `policy.json`, the harness
wheel, the source snapshots and their manifests, the evolved build and the verifier
environment. The baseline build lives in the [store](../store.md), never in another session.
See [file formats](../output-format.md#directory-contents).

A finished evolved build is kept on retry. An incomplete evolved build is archived as
`*.failed-<id>` and rebuilt. A partial snapshot without a committed snapshot manifest is
removed and copied again. An interrupted stored build without `READY` is repaired under its
key lock. Source folders must remain available for snapshots that have not yet completed.

## How long it takes

Build times were measured on an earlier recorded run (Apple M1 Max, 10 cores, macOS, Rust
1.89, Qiskit 2.6.0.dev0 source). That run used Qiskit's fat-LTO profile and built the
revisions one after another; neither applies any more, and the thin-LTO concurrent build has
not been timed yet, so expect considerably less than this:

| Step | Time |
| --- | --- |
| Snapshot of one Qiskit source tree | Seconds |
| Baseline build (venv + dependencies + release Rust compile + install) | **24 min** |
| Evolved build | **52 min** |
| Verifier env | ~45 s |

Nearly all of each build is the release compile of Qiskit's Rust extension
(`qiskit._accelerate`). Creating the venv and installing locked dependencies takes seconds
when pip's cache is warm. The two builds of the same source took very different times, so
treat the numbers as a range: roughly **20–60 minutes per Qiskit build** on a laptop,
depending on load. Each build compiles from scratch in its own target directory and
downloads its own crates.

What this means for your sessions:

| Situation | Builds compiled from scratch |
| --- | --- |
| First session against a store | 2 (baseline into the store, evolved in the session) |
| New session, same baseline content, harness, lock files, toolchain and Python | 1 (evolved); the baseline build is reused with no work |
| New session after a harness edit | 1 (evolved); the baseline gets a new venv from the stored wheel, without a Rust compile |
| `compile` run again on its unfinished session | None for a finished evolved build; a half-finished evolved build directory is renamed `*.failed-<id>` and rebuilt. A baseline build without `READY` is rebuilt under its key lock |

A build command that exceeds its timeout fails `compile` (exit 40) with the path to
`build.log`, and `decide` reports `ERROR`.

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
