"""Streaming structural grading. This package must never import Qiskit."""

from qtb.canonical import numeric
from qtb.errors import HarnessError

DIRECTIVES = {"barrier"}
CONTROL_FLOW = {"if_else", "for_loop", "while_loop", "switch_case"}
LOOSE_IMPLICIT = {
    "measure": (1, 1, []),
    "reset": (1, 0, []),
    "delay": (1, 0, ["duration"]),
    **{name: (None, None, []) for name in CONTROL_FLOW},
}


def d2_n2(operations, native_2q_names):
    levels, count = {}, 0
    for name, qubits, clbits, _params, _payload in operations:
        if name in CONTROL_FLOW:
            raise HarnessError("D2 is undefined for dynamic circuits")
        wires = [("q", q) for q in qubits] + [("c", c) for c in clbits]
        counted = (
            name in native_2q_names
            and len(qubits) == 2
            and name not in {"barrier", "delay", "measure", "reset"}
        )
        new = max((levels.get(w, 0) for w in wires), default=0) + int(counted)
        for wire in wires:
            levels[wire] = new
        count += counted
    return max(levels.values(), default=0), count


def layout_errors(layout, n_in, n_out, requested=None):
    if layout is None:
        if n_in != n_out:
            return ["Missing layout on a widened circuit"]
        initial = final = routing = list(range(n_out))
    else:
        if (layout.get("input_num_qubits"), layout.get("output_num_qubits")) != (n_in, n_out):
            return ["Layout width mismatch"]
        initial = layout.get("initial_index_layout", [])
        final = layout.get("final_index_layout", [])
        routing = layout.get("routing_permutation", [])
    errors = []
    for name, values in (("initial", initial), ("final", final), ("routing", routing)):
        if any(type(v) is not int for v in values) or sorted(values) != list(range(n_out)):
            errors.append(f"{name} layout is not a permutation")
    if errors:
        return errors
    if any(final[i] != routing[initial[i]] for i in range(n_out)):
        errors.append("Final layout disagrees with routing permutation")
    if requested is not None and initial[:n_in] != requested:
        errors.append("Explicit initial layout was not honored")
    return errors


class StructuralChecker:
    """Consume each operation once while keeping O(wires + target) state."""

    def __init__(self, header, target, constraint_form="target"):
        self.header, self.target = header, target
        self.levels, self.n2, self.errors = {}, 0, []
        self.active_qubits = set()
        self.dynamic = False
        self.support = {
            i["name"]: (i, None if i["qargs"] is None else {tuple(q) for q in i["qargs"]})
            for i in target["instructions"]
        }
        self.implied_loose = set()
        if constraint_form == "loose":
            for name, (arity, _clbits, parameters) in LOOSE_IMPLICIT.items():
                if name not in self.support:
                    self.support[name] = (
                        {"name": name, "arity": arity, "parameters": parameters},
                        None,
                    )
                    self.implied_loose.add(name)
        if header["num_qubits"] != target["num_qubits"]:
            self.errors.append("Output width differs from target width")

    def consume(self, op, qmap=None, cmap=None):
        if not isinstance(op, list) or len(op) != 5:
            self.errors.append("Malformed operation")
            return
        name, qs, cs, params, payload = op
        if any(type(q) is not int or not 0 <= q < self.header["num_qubits"] for q in qs):
            self.errors.append("Invalid qubit index")
            return
        if any(type(c) is not int or not 0 <= c < self.header["num_clbits"] for c in cs):
            self.errors.append("Invalid classical index")
            return
        if len(set(qs)) != len(qs) or len(set(cs)) != len(cs):
            self.errors.append("Repeated operation wire")
            return
        qs = [qmap[q] for q in qs] if qmap else qs
        cs = [cmap[c] for c in cs] if cmap else cs
        if name != "barrier":
            self.active_qubits.update(qs)
        if name not in DIRECTIVES:
            entry = self.support.get(name)
            if entry is None:
                self.errors.append(f"Unsupported instruction {name}")
            else:
                spec, allowed = entry
                if spec["arity"] is not None and len(qs) != spec["arity"]:
                    self.errors.append(f"Wrong arity: {name}")
                if name in self.implied_loose:
                    expected_clbits = LOOSE_IMPLICIT[name][1]
                    if expected_clbits is not None and len(cs) != expected_clbits:
                        self.errors.append(f"Wrong classical arity: {name}")
                if allowed is not None and tuple(qs) not in allowed:
                    self.errors.append(f"Illegal ordered qubits: {name}{qs}")
                if name not in CONTROL_FLOW and len(params) != len(spec.get("parameters", [])):
                    self.errors.append(f"Wrong parameter count: {name}")
                for index, param in enumerate(params):
                    if isinstance(param, dict):  # expression/opaque symbolic output
                        if param.get("kind") not in {"expression/1", "symbolic/1"}:
                            self.errors.append(f"Invalid symbolic parameter: {name}")
                        continue
                    try:
                        value = numeric(param)
                        bounds = spec.get("angle_bounds", [])
                        if bounds and bounds[index] is not None:
                            lo, hi = map(numeric, bounds[index])
                            if not lo <= value <= hi:
                                self.errors.append(f"Parameter outside bounds: {name}")
                        fixed = spec.get("fixed_parameters", {})
                        if str(index) in fixed and value != numeric(fixed[str(index)]):
                            self.errors.append(f"Fixed parameter mismatch: {name}")
                    except (ValueError, TypeError, HarnessError, IndexError):
                        self.errors.append(f"Invalid numeric parameter: {name}")
        if name in CONTROL_FLOW:
            self.dynamic = True
            if not payload or payload.get("kind") != "control_flow/1":
                self.errors.append("Missing control-flow payload")
            else:
                for block in payload["blocks"]:
                    for child in block["operations"]:
                        self.consume(child, qs, cs)
            return
        wires = [("q", q) for q in qs] + [("c", c) for c in cs]
        counted = name in self.target["native_2q_names"] and len(qs) == 2
        new = max((self.levels.get(w, 0) for w in wires), default=0) + int(counted)
        for wire in wires:
            self.levels[wire] = new
        self.n2 += counted

    def result(self):
        result = {"status": "mismatch" if self.errors else "verified", "errors": self.errors}
        if not self.dynamic:
            result.update(D2=max(self.levels.values(), default=0), N2=self.n2)
        return result
