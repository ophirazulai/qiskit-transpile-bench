"""Regenerate the version-1 JSON contracts; no Qiskit import."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/qtb/config/schemas"
S = {"type": "string"}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
INT = {"type": "integer", "minimum": 0}


def obj(properties, required=None, extra=False):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": extra,
    }


def array(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum}


def enum(*values):
    return {"enum": list(values)}


ART = obj({"file": S, "sha256": HASH}, extra=True)
REF = {
    "oneOf": [
        obj({"kind": {"const": "input"}}),
        obj({"kind": {"const": "frozen_circuit"}, "contract": S, "file": S, "sha256": HASH}),
    ]
}
CASE = obj(
    {
        "case_id": S,
        "role": enum(
            "scored", "guard", "deterministic", "zero_baseline", "canary", "timing", "memory"
        ),
        "family": S,
        "size_band": S,
        "topology": S,
        "native_basis": S,
        "input_group": S,
        "variant": S,
        "optimization_level": enum(None, 0, 1, 2, 3),
        "logical_qubits": INT,
        "active_qubits": INT,
        "seeds_per_block": {"type": "integer", "minimum": 1},
        "circuit": ART,
        "target": ART,
        "semantic_reference": REF,
        "input_domain": enum("all_zero", "all_inputs", "selected_states"),
        "constraint_form": enum("target", "loose"),
        "options": {"type": "object"},
        "weight": {"type": "number", "minimum": 0},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0},
        "modes": array(S, 1),
    },
    extra=True,
)
RECORD = obj(
    {
        "id": S,
        "kind": enum("harness", "correctness", "guard", "cost", "improvement", "completeness"),
        "subject": enum("evolved", "reference"),
        "result": enum("passed", "passed_on_rerun", "failed", "unresolved", "not_evaluated"),
    },
    extra=True,
)
SCHEMAS = {
    "manifest": obj(
        {
            "format": {"const": "qtb-manifest/1"},
            "profile": S,
            "version": INT,
            "cases": array(CASE, 1),
        },
        extra=True,
    ),
    "policy": obj(
        {
            "format": {"const": "qtb-policy/1"},
            "profile": S,
            "version": INT,
            "required_ids": array(S, 1),
            "quality_prerequisites": array(S, 1),
            "practical_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            "quality_cap": {"type": "number", "minimum": 1},
            "improvement_multiplier": {"type": "number", "exclusiveMinimum": 0},
            "guard_multiplier": {"type": "number", "exclusiveMinimum": 0},
            "measurement_protocol": {"type": "object"},
            "bootstrap_replicates": INT,
            "rng_seed": INT,
            "iterations_groups": array(S),
        },
        extra=True,
    ),
    "job": obj(
        {
            "protocol": {"const": "qtb-worker/1"},
            "mode": enum(
                "roundtrip",
                "quality",
                "prefix",
                "timing_e2e",
                "timing_reuse",
                "preset_build",
                "memory",
                "diagnostics",
                "api_checks",
            ),
            "case": CASE,
            "fixture_root": S,
            "seeds": array(INT, 1),
            "build": {"type": "object"},
            "timeout_s": {"type": "number", "exclusiveMinimum": 0},
        },
        extra=True,
    ),
    "result": obj(
        {
            "protocol": {"const": "qtb-worker/1"},
            "status": enum("ok", "unsupported", "error"),
            "mode": S,
            "seed": INT,
        },
        extra=True,
    ),
    "observation": obj(
        {
            "format": {"const": "qtb-observation/1"},
            "id": HASH,
            "case_id": S,
            "revision": enum("baseline", "evolved"),
            "build_id": HASH,
            "case_hash": HASH,
            "seed": INT,
            "seed_block": enum("B0", "KB1", "KB2"),
            "mode": S,
        },
        extra=True,
    ),
    "constraint": RECORD,
    "decision": obj(
        {
            "format": {"const": "qtb-decision/1"},
            "status": enum(
                "PASS", "NO_IMPROVEMENT", "CONSTRAINT_VIOLATION", "INCONCLUSIVE", "ERROR"
            ),
            "improved_under_constraints": {"type": ["boolean", "null"]},
            "profile": S,
            "hashes": {"type": "object"},
            "constraints": array(RECORD),
            "required_ids": array(S, 1),
            "seed_block": {"const": "B0"},
        },
        extra=True,
    ),
}
for name, schema in SCHEMAS.items():
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://qiskit-transpile-bench.invalid/schemas/{name}/1"
    (ROOT / f"{name}.json").write_text(json.dumps(schema, indent=2) + "\n")
