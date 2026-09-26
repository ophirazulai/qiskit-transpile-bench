# Cluster execution

[Documentation index](README.md) · [Session rules](sessions.md) · [cost](workflow/cost.md)

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
