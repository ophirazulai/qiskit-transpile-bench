"""Pinned oracle subprocess. Inputs and outputs are files, never live objects."""

import argparse

import qiskit

from qtb.canonical import read_circuit, read_json, write_json
from qtb.errors import HarnessError
from qtb_verifier.importer import import_circuit
from qtb_verifier.layout_semantics import verify_zero
from qtb_verifier.small_exact import verify_clifford, verify_unitary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if qiskit.__version__ != "2.5.2":
        raise HarnessError("Verifier must run the independently pinned Qiskit 2.5.2")
    job = read_json(args.job)
    if job["protocol"] != "qtb-verifier/1":
        raise HarnessError("Unknown verifier protocol")

    def load(path):
        header, operations = read_circuit(path)
        return import_circuit({"header": header, "operations": operations})

    try:
        reference, output = load(job["reference"]), load(job["output"])
        if job["oracle"] == "C1":
            result = verify_unitary(reference, output, job["layout"], job["input_domain"])
        elif job["oracle"] == "C7":
            result = verify_clifford(
                reference, output, job["layout"], job.get("covers"), job.get("substituted", [])
            )
        elif job["oracle"] in {"C2", "C1-lite"}:
            result = verify_zero(reference, output, job["layout"], job.get("max_qubits", 25))
        elif job["oracle"] == "C3":
            from qtb_verifier.dynamic import verify_dynamic
            from qtb_verifier.layout_semantics import zero_reference

            result = verify_dynamic(
                zero_reference(reference, output.num_qubits, job["layout"]), output
            )
        elif job["oracle"] == "C5":
            from qtb.canonical import numeric
            from qtb_verifier.schedule import verify_schedule

            target = read_json(job["target"])
            _header, operations = read_circuit(job["output"])
            dt = numeric(target["dt"])
            support = {
                s["name"]: {tuple(p["qargs"]): p for p in s["properties"] if p["qargs"] is not None}
                for s in target["instructions"]
            }
            durations = []
            for name, qs, _cs, params, payload in operations:
                if name == "barrier":
                    durations.append(0)
                elif name == "delay":
                    if payload["unit"] != "dt":
                        raise HarnessError("Scheduled delays must be in dt units")
                    durations.append(numeric(params[0]))
                else:
                    durations.append(round(numeric(support[name][tuple(qs)]["duration"]) / dt))
            result = verify_schedule(operations, job["start_times_dt"], durations, target)
        else:
            raise HarnessError("Unsupported verifier oracle")
    except Exception as exc:
        result = {"status": "unverified", "detail": f"{type(exc).__name__}: {exc}"}
    result.update(protocol="qtb-verifier/1", reference_hash=job["reference_hash"])
    write_json(args.out, result)


if __name__ == "__main__":
    main()
