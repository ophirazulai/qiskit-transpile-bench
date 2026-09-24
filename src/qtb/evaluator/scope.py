"""Conservative, level-aware changed-stage mapping and semantic coverage."""

from qtb.config import STAGES
from qtb.errors import HarnessError


def changed_scope(paths, level, declaration=()):
    if not set(declaration) <= set(STAGES):
        raise HarnessError("Unknown stage in scope declaration")
    stages, components, unknown = set(declaration), set(), []
    for path in paths:
        path = path.lower()
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
        if check.get("reference_hash") != reference_hash:
            continue
        if check.get("input_domain") not in {case["input_domain"], "all_inputs"}:
            continue
        if not set(scope["stages"]) <= set(check.get("covers", [])):
            continue
        if set(scope["components"]) & set(check.get("substituted", [])):
            continue
        return True
    return False
