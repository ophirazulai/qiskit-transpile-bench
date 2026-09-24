"""Compile and export raw evidence; no quality or semantic grading here."""

import resource
import sys
import time
from pathlib import Path

from qtb.canonical import digest, file_hash, read_circuit, read_json, verify_artifact, write_circuit
from qtb.errors import HarnessError
from qtb_worker.adapter import (
    compile_options,
    export_circuit,
    export_layout,
    export_target,
    fingerprint,
    import_circuit,
    import_target,
    preset,
)


def verify_provenance(build):
    import qiskit
    import qiskit._accelerate as native

    root = Path(sys.prefix).resolve()
    for path in (qiskit.__file__, native.__file__):
        if not Path(path).resolve().is_relative_to(root):
            raise HarnessError(f"Qiskit import escaped environment: {path}")
    expected = build["provenance"]
    if file_hash(native.__file__) != expected["native_sha256"]:
        raise HarnessError("Native extension hash differs from build provenance")


def load_inputs(job):
    case, root = job["case"], job["fixture_root"]
    circuit_path = verify_artifact(root, case["circuit"], circuit=True)
    target_path = verify_artifact(root, case["target"])
    header, operations = read_circuit(circuit_path)
    circuit = import_circuit({"header": header, "operations": operations})
    target_data = read_json(target_path)
    return circuit, import_target(target_data), target_data


def save_output(output, input_width, path, opaque=False):
    data = export_circuit(output, opaque=opaque)
    hash_ = write_circuit(path, data["header"], data["operations"])
    return {"output": str(path), "output_hash": hash_, "layout": export_layout(output, input_width)}


def run_seed(job, seed, outdir, inputs):
    from qiskit import transpile

    circuit, target, target_data = inputs
    case, mode = job["case"], job["mode"]
    outdir = Path(outdir)
    if mode == "roundtrip":
        data = export_circuit(circuit)
        hash_ = write_circuit(
            outdir / f"input-{seed}.ops.jsonl.gz", data["header"], data["operations"]
        )
        return {
            "circuit_hash": hash_,
            "target_hash": digest(export_target(target, target_data["native_2q_names"])),
        }
    if mode in {"quality", "prefix", "diagnostics"}:
        pm = preset(case, target, seed, job.get("pipeline_edits", []))
        trace = []

        def callback(**kwargs):
            trace.append(
                {
                    "pass": type(kwargs["pass_"]).__name__,
                    "seconds": kwargs["time"],
                    "count": kwargs["count"],
                }
            )

        output = pm.run(circuit, **({"callback": callback} if mode == "diagnostics" else {}))
        result = save_output(
            output,
            circuit.num_qubits,
            outdir / f"output-{seed}.ops.jsonl.gz",
            opaque=bool(output.parameters),
        )
        result["fingerprint"] = fingerprint(pm)
        if case["options"].get("scheduling_method"):
            result["start_times_dt"] = list(output.op_start_times)
        if trace:
            result["diagnostics"] = trace
        if output.parameters:
            result["free_parameters"] = sorted(p.name for p in output.parameters)
            result["bindings"] = []
            for index, binding in enumerate(job.get("bindings", [])):
                bound = output.assign_parameters({p: binding[p.name] for p in output.parameters})
                result["bindings"].append(
                    save_output(
                        bound, circuit.num_qubits, outdir / f"bound-{seed}-{index}.ops.jsonl.gz"
                    )
                )
        return result
    if mode in {"timing_e2e", "timing_reuse", "preset_build", "memory"}:
        pm = preset(case, target, seed) if mode in {"timing_reuse", "memory"} else None
        if mode == "memory":
            setup = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            pm.run(circuit)
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            scale = 1 if sys.platform == "darwin" else 1024
            return {"setup_peak_rss_bytes": int(setup * scale), "peak_rss_bytes": int(peak * scale)}

        def call():
            if mode == "timing_reuse":
                return pm.run(circuit)
            if mode == "preset_build":
                return preset(case, target, seed)
            return transpile(circuit, **compile_options(case, target, seed))

        for _ in range(job.get("warmups", 1)):
            call()
        samples, elapsed = [], 0
        while len(samples) < job.get("minimum_calls", 3) or elapsed < job.get("minimum_ns", 10**9):
            start = time.perf_counter_ns()
            call()
            duration = time.perf_counter_ns() - start
            samples.append(duration)
            elapsed += duration
        return {"samples_ns": samples}
    if mode == "api_checks":
        before = digest(export_circuit(circuit))
        pm = preset(case, target, seed)
        first = pm.run(circuit)
        other = circuit.copy()
        other.x(0)
        pm.run(other)
        again = pm.run(circuit)
        batch = pm.run([circuit, other])
        return {
            "input_before": before,
            "input_after": digest(export_circuit(circuit)),
            "first": digest(export_circuit(first, opaque=True)),
            "again": digest(export_circuit(again, opaque=True)),
            "batch_count": len(batch),
            "batch_hashes": [digest(export_circuit(c, opaque=True)) for c in batch],
            "individual_hashes": [
                digest(export_circuit(pm.run(c), opaque=True)) for c in (circuit, other)
            ],
        }
    raise HarnessError(f"Unsupported worker mode: {mode}")
