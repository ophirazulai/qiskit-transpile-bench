# Architecture and components

[Documentation index](README.md)

This page explains how the harness is put together: which parts exist, what each one is
allowed to do, and how isolated processes exchange evidence. For the reasoning behind every
rule, see the design plans in `design/transpilation-benchmark-impl-plan.md` and
`design/split-compare-stages-plan.md`. For what is implemented and validated, see
[implementation and runner validation](validation.md).

## The problem it solves

You have two Qiskit source folders: a **baseline** (the reference) and an **evolved** revision
(a candidate change to the transpiler). The question is:

> Does the evolved revision produce circuits with lower native two-qubit depth on a fixed
> workload, without breaking correctness, getting worse on other quality measures, or
> becoming slower or larger to compile?

Answering this fairly is hard because the candidate is itself Qiskit. If the candidate
computed its own score (for example with `QuantumCircuit.depth()`), a change could improve its
score without improving any circuit. The architecture exists mainly to prevent that.

## Trust boundaries

Three rules shape the whole design:

1. **Never load two Qiskit versions in one process, and never pass live Qiskit objects
   between processes.** Components talk only through versioned JSON and JSONL files.
2. **The revision under test never grades itself.** The worker inside a revision's
   environment only compiles and exports. Metrics, legality, routing replay and semantic
   checks are computed elsewhere: by pure-Python harness code or by an independently pinned
   Qiskit (the verifier).
3. **Evaluation is replayable.** The verdict is computed from saved observations and
   evidence. `decide` recomputes it at any time from the session's files and archived
   profile, without compiling anything, also after `clean`.

```text
  baseline folder ─┐                               ┌─ profile: manifest.json, policy.json
  evolved folder  ─┼─► snapshot ─► build wheels ───┤     fixtures (circuits, targets)
                   │   (envbuild)  (2 venvs)       │
                   │                               ▼
                   │                         ┌────────────┐
                   │        job.json ───────►│ coordinator│◄──── never imports Qiskit
                   │                         └─────┬──────┘
                   │             ┌─────────────────┴─────────────────┐
                   ▼             ▼                                   ▼
          worker (baseline venv)  worker (evolved venv)         verifier venv
          imports that revision   imports that revision         Qiskit 2.5.2
                   │                    │                            │
                   └─ outputs, layouts, timings                      oracle results
                                        │                            │
                                        ▼                            │
                         observations.jsonl, evidence.json  ◄────────┘
                                        │
                                        ▼
                         evaluator ─► reporter ─► decision.json + report.md
```

## Components

| Component | Location | Imports Qiskit? | Responsibility |
| --- | --- | --- | --- |
| CLI | `src/qtb/cli.py` | No | `compile`, `quality`, `correctness`, `unit-tests`, `cost`, `decide`, `clean`; exit codes |
| Profile configuration | `profiles/*/manifest.json`, `policy.json`, `src/qtb/config/` | No | Versioned workloads (cases, roles, weights, seeds) and thresholds; JSON Schemas for every file format |
| Fixtures | `fixtures/` | No | Frozen canonical circuits, targets, semantic references, the C1–C5 correctness suite, provenance and licenses |
| Canonical IO | `src/qtb/canonical/` | No | Deterministic JSON, hex floats, gzip circuit files, SHA-256 hashing, atomic writes |
| Environment builder | `src/qtb/envbuild/` | No | Snapshot source folders, build release wheels in isolated venvs, record provenance ([environments.md](environments.md)) |
| Coordinator | `src/qtb/coordinator/` | No | Stages and their locks, the gate, the baseline store, job scheduling, timeouts, determinism audit, cost sessions, upstream tests, `decide` and `clean` |
| Worker | `worker/qtb_worker/` | Yes, the revision under test | Rebuild inputs from canonical data, compile, export outputs, time compiles, measure memory, run live API checks |
| Metrics | `src/qtb/metrics/` | No | Streaming `D2`/`N2`, target legality (C0), layout validation, exact routing replay (C6) |
| Verifier | `verifier/qtb_verifier/` | Yes, pinned Qiskit 2.5.2 | Semantic oracles: C1, C2/C1-lite, C3, C5, C7 ([verifier.md](verifier.md)) |
| Evaluator | `src/qtb/evaluator/` | No | Paired log-ratio estimators, guards, change scope and stage coverage, cost guards, verdict ([metrics.md](metrics.md)) |
| Reporter | `src/qtb/reporter/` | No | Writes `decision.json` and `report.md` ([output-format.md](output-format.md)) |
| Tools | `tools/` | Some | Fixture curation (`tools/curate/`), probes, timeout freezing, schema generation. Not used at comparison time |

The harness ships as one wheel (`qiskit-transpile-bench`) that contains all three Python
packages: `qtb`, `qtb_worker` and `qtb_verifier`. The profiles, fixtures and lock files are
bundled as `qtb/data`. The same wheel is installed into every revision environment and the
verifier environment. Each process uses only the package that belongs to it.

## Process model

The coordinator runs in your `uv` environment and starts every other process itself:

- **Worker:** `<revision-env>/bin/python -P -m qtb_worker --job job.json --out out/`.
  It runs from a scratch directory, with a sanitized environment and a serial configuration
  (`QISKIT_PARALLEL=FALSE`, `RAYON_NUM_THREADS=1`, `PYTHONHASHSEED=0` and so on; see
  [environments.md](environments.md#runtime-environment-of-a-worker)). Before doing anything,
  it checks that `qiskit` and `qiskit._accelerate` load from inside its own venv and that
  the native extension's SHA-256 matches the build record.
- **Verifier:** `<verifier-env>/bin/python -P -m qtb_verifier --job job.json --out result.json`.
  It refuses to run unless the installed Qiskit is exactly 2.5.2.

A worker job covers one case and a batch of seeds (up to 25 for quality). The worker appends one
JSON line to `out/results.jsonl` per finished seed and fsyncs it. The coordinator treats each
line as a heartbeat. If no new line appears within the case's `timeout_s`, it kills the
process group, records the stuck seed as an error, and reruns the remaining seeds in a fresh
process.

### Worker modes

| Mode | What the worker does | Output |
| --- | --- | --- |
| `roundtrip` | Rebuild the input circuit and target from canonical data, then export them again | Hashes that must equal the frozen fixture hashes |
| `quality` | Build the preset pass manager for (target, options, seed), compile, export | Canonical output circuit, layout, pipeline fingerprint, compile time |
| `prefix` | Like `quality`, with `pipeline_edits` such as `drop_stage:optimization` | Truncated-pipeline outputs for routing replay and Clifford checks |
| `timing_e2e` | Warm up, then time complete `transpile()` calls | Raw nanosecond samples |
| `timing_reuse` | Build the pass manager outside the clock, then time `pm.run()` | Raw samples |
| `timing_batch` | Run every `timing_e2e`, `timing_reuse` and `preset_build` case of a panel in one process, one result row per case | Raw samples per case |
| `preset_build` | Time `generate_preset_pass_manager(...)` alone | Raw samples |
| `memory` | One compile in a fresh process | Setup and peak RSS in bytes |
| `diagnostics` | One untimed compile with a pass callback | Per-pass times (report-only) |
| `api_checks` | Pass-manager contracts that need live objects | Input immutability, reuse without leaks, batch order, expected errors |

## Workflow and ownership

The [workflow guide](README.md#workflow) documents each command. Common locks, prerequisites
and retry rules live in [sessions](sessions.md). Each stage owns its state, evidence and log;
`compile` alone writes `run.json`, and `decide` alone writes the combined evidence and verdict.
[Output formats](output-format.md#directory-contents) lists file ownership.

The baseline build and baseline results are shared through the [store](store.md). Everything
about the evolved tree and the trusted verifier stays in the session. Cost samples always
come from fresh two-arm measurements in that session.

Each stage can run on its own compatible host. Only cost requires a quiet, exclusive host;
see [cluster execution](cluster.md).

## Source layout

```text
src/qtb/
  cli.py              command line: the seven commands and their exit codes
  canonical/          hashing and file formats
  config/             profile loading, identities, JSON Schemas (schemas/*.json)
  envbuild/           snapshot, build, provenance
  coordinator/
    __init__.py       Comparison: session, builds, quality, audit, routing replay,
                      aggregation, the gate
    stages.py         running a stage: locks, preconditions, state.json, stage bodies
    decide.py         decide: evidence merge, stage rules, verdict and report
    clean.py          clean: planned deletions and clean.json
    store.py          the baseline store: build entries, result entries
    checks.py         C1–C5 behavior suite, API checks, C7 Clifford checks
    costs.py          panel selection by scope, interleaved two-arm cost sessions, thresholds
    upstream.py       baseline-owned Python tests and Rust tests
    process.py        worker subprocess with heartbeats and timeouts
    storage.py        locks, JSONL records, cache keys, output pruning
    runlog.py         progress logs and step durations
  metrics/            D2/N2, C0 legality, layout validation, C6 replay
  evaluator/          statistics, quality guards, cost guards, scope, verdict
  reporter/           decision.json and report.md
worker/qtb_worker/    adapter.py (Qiskit version adapter), modes.py
verifier/qtb_verifier/ small_exact.py (C1, C7), layout_semantics.py (C2, C1-lite),
                      dynamic.py (C3), schedule.py (C5)
profiles/             iterations-profile/, confirm-profile/
fixtures/             circuits/, references/, targets/, correctness-suite.json, PROVENANCE.md
envs/                 lock files for revision, test and verifier environments
tools/                curation, probes, timeout freezing, schema generation, lsf/ examples
tests/                harness unit tests
design/               design plan and probes
```
