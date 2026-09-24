"""Measure baseline compiles and draft a new manifest with frozen case timeouts.

Measure on the intended controlled runner after building the baseline once::

    uv run python tools/freeze_timeouts.py measure --run results/runs/RUN \
        --profile confirm-profile --out results/timeout-probes/confirm.jsonl
    uv run python tools/freeze_timeouts.py freeze --profile confirm-profile \
        --measurements results/timeout-probes/confirm.jsonl \
        --out results/timeout-probes/confirm-manifest.json

The draft must be reviewed and qualified before replacing a shipped profile. A
measurement is made for every seed of every quality case, and at the fixed seed
for cost-only cases. Failed or missing measurements prevent freezing.
"""

import argparse
import json
import math
import shutil
import tempfile
from pathlib import Path

from qtb.canonical import digest, read_json, write_json
from qtb.config import load_profile, validate
from qtb.coordinator.process import run_worker
from qtb.coordinator.storage import append_record, read_records
from qtb.errors import HarnessError


def required_seeds(case):
    if case["role"] in {"timing", "memory"}:
        return [case.get("timing", {}).get("fixed_seed", 0)]
    return list(range(case["seeds_per_block"]))


def freeze(manifest, records, *, source_hash):
    """Return a versioned draft only when every required baseline seed succeeded."""
    expected = {(c["case_id"], s) for c in manifest["cases"] for s in required_seeds(c)}
    measured = {}
    for row in records:
        key = row["case_id"], row["seed"]
        if key not in expected:
            raise HarnessError(f"Unexpected timeout measurement: {key}")
        if key in measured:
            raise HarnessError(f"Duplicate timeout measurement: {key}")
        if row.get("status") != "ok":
            raise HarnessError(f"Failed timeout measurement: {key}: {row.get('error')}")
        ns = row.get("compile_ns")
        if isinstance(ns, bool) or not isinstance(ns, int) or ns <= 0:
            raise HarnessError(f"Missing positive compile_ns: {key}")
        measured[key] = ns
    missing = expected - measured.keys()
    if missing:
        sample = sorted(missing)[:3]
        raise HarnessError(
            f"Incomplete baseline timeout measurements: {len(missing)} missing; {sample}"
        )
    frozen = json.loads(json.dumps(manifest))
    frozen["version"] += 1
    frozen["status"] = "unqualified"
    frozen["timeout_source"] = {
        "rule": "max(120 s, 10 x slowest measured baseline compile)",
        "measurements_sha256": source_hash,
    }
    for case in frozen["cases"]:
        slowest_ns = max(measured[(case["case_id"], seed)] for seed in required_seeds(case))
        # Whole seconds avoid rounding a measured duration below the required factor.
        case["timeout_s"] = max(120, math.ceil(10 * slowest_ns / 1_000_000_000))
    validate("manifest", frozen)
    return frozen


def measure(run_dir, profile, out, probe_timeout_s):
    run_dir, out = Path(run_dir).resolve(), Path(out).resolve()
    run = read_json(run_dir / "run.json")
    manifest, _, hashes = load_profile(profile)
    if run["hashes"]["manifest"] != hashes["manifest"]:
        raise HarnessError("Baseline run used a different manifest")
    if "baseline" not in run.get("builds", {}):
        raise HarnessError("Baseline run has no completed baseline build")
    out.parent.mkdir(parents=True, exist_ok=True)
    build = run["builds"]["baseline"]
    existing = read_records(out)
    seen = set()
    for row in existing:
        key = row["case_id"], row["seed"]
        if (
            key in seen
            or row.get("manifest_hash") != hashes["manifest"]
            or row.get("baseline_build_id") != build["id"]
            or row.get("status") != "ok"
            or not isinstance(row.get("compile_ns"), int)
            or row["compile_ns"] <= 0
        ):
            raise HarnessError(f"Invalid prior timeout measurement: {key}")
        seen.add(key)
    fixture_root = Path(__file__).resolve().parents[1] / "fixtures"
    with tempfile.TemporaryDirectory(prefix="qtb-timeout-probes-") as temp:
        for index, case in enumerate(manifest["cases"], 1):
            print(f"{index}/{len(manifest['cases'])}: {case['case_id']}", flush=True)
            missing = [
                seed for seed in required_seeds(case) if (case["case_id"], seed) not in seen
            ]
            for batch_index, start in enumerate(range(0, len(missing), 25)):
                seeds = missing[start : start + 25]
                job = {
                    "mode": "quality",
                    "case": dict(case, timeout_s=probe_timeout_s),
                    "seeds": seeds,
                    "fixture_root": str(fixture_root),
                    "timeout_s": probe_timeout_s,
                    "pipeline_edits": [],
                    "bindings": [],
                }
                directory = Path(temp) / f"job-{index}-{batch_index}"
                rows = run_worker(build, job, directory)
                for row in rows:
                    if row["status"] != "ok" or "compile_ns" not in row:
                        raise HarnessError(
                            f"Baseline timeout probe failed: {case['case_id']} "
                            f"seed {row['seed']}: {row.get('error', 'missing compile_ns')}"
                        )
                    append_record(
                        out,
                        {
                            "case_id": case["case_id"],
                            "seed": row["seed"],
                            "status": "ok",
                            "compile_ns": row["compile_ns"],
                            "baseline_build_id": build["id"],
                            "manifest_hash": hashes["manifest"],
                        },
                    )
                    seen.add((case["case_id"], row["seed"]))
                shutil.rmtree(directory)
    print(f"Measurements: {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--run", required=True)
    m.add_argument("--profile", choices=("iterations-profile", "confirm-profile"), required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--probe-timeout-s", type=float, default=3600)
    f = sub.add_parser("freeze")
    f.add_argument("--profile", choices=("iterations-profile", "confirm-profile"), required=True)
    f.add_argument("--measurements", required=True)
    f.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.action == "measure":
        measure(args.run, args.profile, args.out, args.probe_timeout_s)
    else:
        manifest, _, hashes = load_profile(args.profile)
        path = Path(args.measurements).resolve()
        records = read_records(path)
        if not records or any(row.get("manifest_hash") != hashes["manifest"] for row in records):
            raise HarnessError("Timeout measurements do not match this manifest")
        build_ids = {row.get("baseline_build_id") for row in records}
        if len(build_ids) != 1 or None in build_ids:
            raise HarnessError("Timeout measurements require one identified baseline build")
        result = freeze(manifest, records, source_hash=digest(records))
        out = Path(args.out).resolve()
        if out.exists():
            raise HarnessError(f"Refusing to replace existing manifest: {out}")
        write_json(out, result)
        print(f"Draft manifest: {out}")


if __name__ == "__main__":
    main()
