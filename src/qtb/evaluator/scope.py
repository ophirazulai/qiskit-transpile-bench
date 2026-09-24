"""Conservative, level-aware changed-stage mapping and semantic coverage."""

from qtb.config import STAGES
from qtb.errors import HarnessError


def _non_source_path(path):
    """Paths in a snapshot diff that cannot run in the transpilation pipeline."""
    parts = path.split("/")
    name = parts[-1]
    if any(
        part
        in {
            "test",
            "tests",
            "docs",
            "doc",
            "releasenotes",
            ".github",
            ".circleci",
            ".buildkite",
            "ci",
        }
        for part in parts[:-1]
    ):
        return True
    if name in {
        "cargo.lock",
        "uv.lock",
        "pdm.lock",
        "poetry.lock",
        "changelog",
        "readme",
        "license",
        "copying",
        ".gitlab-ci.yml",
        ".travis.yml",
        "azure-pipelines.yml",
        "tox.ini",
        "noxfile.py",
    }:
        return True
    return name.endswith((".md", ".rst"))


def changed_scope(paths, level, declaration=()):
    if not set(declaration) <= set(STAGES):
        raise HarnessError("Unknown stage in scope declaration")
    stages, components, unknown = set(declaration), set(), []
    for path in paths:
        path = path.lower().replace("\\", "/")
        if _non_source_path(path):
            continue
        if not (path.startswith("qiskit/") or (path.startswith("crates/") and "/src/" in path)):
            stages.update(STAGES)
            unknown.append(path)
            continue
        if "sabre" in path and ("passes/" in path or "routing/" in path or "layout/" in path):
            stages.update(["layout", "routing"])
        elif "vf2" in path:
            stages.update(["layout", "routing"] + (["optimization"] if level == 3 else []))
        elif any(s in path for s in ("two_qubit_decompose", "unitary_synthesis", "weyl")):
            stages.update(["init", "translation", "optimization"])
            components.add("unitary_synthesis")
        elif "consolidate_blocks" in path:
            stages.add("init")
        elif any(s in path for s in ("commutative_cancellation", "remove_identity")):
            stages.update(["init", "optimization"])
        elif "optimization/" in path:
            # Optimization passes can also be invoked from init.
            stages.update(["init", "optimization"])
        else:
            stages.update(STAGES)
            unknown.append(path)
    return {"stages": sorted(stages), "components": sorted(components), "unmapped_paths": unknown}


def covered(case, checks, scope):
    reference = case["semantic_reference"]
    reference_hash = (
        case["circuit"]["sha256"] if reference["kind"] == "input" else reference["sha256"]
    )
    for check in checks:
        if check.get("status") != "verified":
            continue
        if check.get("oracle") == "C7":
            variant = case.get("clifford_variant")
            if not variant or check.get("mode") not in {"full", "prefix"}:
                continue
            expected_hash = variant["sha256"]
        else:
            expected_hash = reference_hash
        if check.get("reference_hash") != expected_hash:
            continue
        if check.get("input_domain") not in {case["input_domain"], "all_inputs"}:
            continue
        if not set(scope["stages"]) <= set(check.get("covers", [])):
            continue
        if set(scope["components"]) & set(check.get("substituted", [])):
            continue
        return True
    return False
