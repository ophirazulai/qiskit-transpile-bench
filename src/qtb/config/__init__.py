"""Strict contracts and versioned profile loading."""

import importlib.util
import math
from importlib.resources import files
from pathlib import Path

import jsonschema

from qtb.canonical import digest, file_hash, read_json, verify_artifact
from qtb.errors import HarnessError

PROTOCOL = "qtb-worker/1"
STAGES = ["init", "layout", "routing", "translation", "optimization", "scheduling"]
ROLES = ["scored", "guard", "deterministic", "zero_baseline", "canary", "timing", "memory"]
MODES = [
    "roundtrip",
    "quality",
    "prefix",
    "timing_e2e",
    "timing_reuse",
    "timing_batch",
    "preset_build",
    "memory",
    "diagnostics",
    "api_checks",
]


def data_root():
    installed = Path(str(files("qtb"))) / "data"
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[3]


def coordinator_identity():
    root = Path(__file__).resolve().parents[1]
    return digest(
        {
            str(p.relative_to(root)): file_hash(p)
            for p in sorted(root.rglob("*.py"))
            if "data" not in p.relative_to(root).parts
        }
    )


def implementation_identity():
    """Code/schema identity excludes profile data so unchanged cases stay reusable."""
    entries = {}
    for package in ("qtb", "qtb_worker", "qtb_verifier"):
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            raise HarnessError(f"Missing installed harness package: {package}")
        root = Path(next(iter(spec.submodule_search_locations)))
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if "data" not in relative.parts and path.suffix in {".py", ".json"}:
                entries[f"{package}/{relative}"] = file_hash(path)
    return digest(entries)


def validate(kind, value):
    schema = read_json(Path(__file__).parent / "schemas" / f"{kind}.json")
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        raise HarnessError(f"Invalid {kind} at {list(exc.path)}: {exc.message}") from exc
    return value


def load_profile(name, root=None, verify=True):
    root = Path(root or data_root())
    if name not in {"iterations-profile", "confirm-profile"}:
        raise HarnessError(f"Unknown profile {name}")
    manifest = validate("manifest", read_json(root / "profiles" / name / "manifest.json"))
    policy = validate("policy", read_json(root / "profiles" / name / "policy.json"))
    if manifest["profile"] != name or policy["profile"] != name:
        raise HarnessError("Profile identity mismatch")
    cases = manifest["cases"]
    if len({c["case_id"] for c in cases}) != len(cases):
        raise HarnessError("Duplicate case ID")
    scored = [c for c in cases if c["role"] == "scored"]
    if not scored or not math.isclose(sum(c["weight"] for c in scored), 1, abs_tol=1e-12):
        raise HarnessError("Scored weights must sum to one")
    if any(c["seeds_per_block"] != 100 for c in scored):
        raise HarnessError("Every scored case requires the entire B0 block")
    if name == "confirm-profile" and {c["family"] for c in scored} != {
        f"G{i}" for i in range(1, 9)
    }:
        raise HarnessError("Confirm requires all eight scored families")
    if len(policy["required_ids"]) != len(set(policy["required_ids"])):
        raise HarnessError("Duplicate required constraint ID")
    if verify:
        checked = set()
        for case in cases:
            artifacts = [(case["circuit"], True), (case["target"], False)]
            if case["semantic_reference"]["kind"] == "frozen_circuit":
                artifacts.append((case["semantic_reference"], True))
            for artifact, circuit in artifacts:
                key = artifact["file"], artifact["sha256"]
                if key not in checked:
                    verify_artifact(root / "fixtures", artifact, circuit)
                    checked.add(key)
    return manifest, policy, {"manifest": digest(manifest), "policy": digest(policy)}


def case_hash(case):
    # Panel membership and weights affect evaluation, not the compiled observation.
    excluded = {"case_id", "weight", "panel", "family", "size_band", "provenance"}
    return digest({k: v for k, v in case.items() if k not in excluded})
