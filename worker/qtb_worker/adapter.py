"""Explicit public-API adapter. Unknown semantics are refused, never decomposed away."""

import math
from fractions import Fraction
from functools import lru_cache

from qtb.canonical import CIRCUIT_FORMAT, TARGET_FORMAT, number, numeric
from qtb.errors import Unsupported


@lru_cache(maxsize=1)
def standard_gates():
    from qiskit.circuit.library import get_standard_gate_name_mapping

    return get_standard_gate_name_mapping()


def encode_value(value, parameters, opaque=False):
    from qiskit.circuit import ParameterExpression

    if isinstance(value, ParameterExpression) and value.parameters:
        if opaque:
            return {
                "kind": "symbolic/1",
                "text": str(value),
                "free_parameters": sorted(p.name for p in value.parameters),
            }
        ids = {p.name: id_ for p, id_ in parameters.items()}

        def tree(expr):
            if expr.is_Symbol:
                return ["parameter", ids[str(expr)]]
            if expr.is_Integer:
                return ["integer", int(expr)]
            if expr.is_Rational:
                return ["rational", int(expr.p), int(expr.q)]
            if expr.is_Float or expr == math.pi:
                return ["float", number(expr)]
            if str(expr) == "pi":
                return ["pi"]
            name = expr.func.__name__
            names = {
                "Add": "add",
                "Mul": "mul",
                "Pow": "pow",
                "sin": "sin",
                "cos": "cos",
                "tan": "tan",
                "exp": "exp",
                "log": "log",
                "Abs": "abs",
                "asin": "arcsin",
                "acos": "arccos",
                "atan": "arctan",
            }
            if name not in names:
                raise Unsupported(f"Unsupported symbolic operation: {name}")
            return [names[name], *[tree(arg) for arg in expr.args]]

        return {"kind": "expression/1", "tree": tree(value.sympify())}
    return number(value)


def decode_value(value, parameters):
    if not isinstance(value, dict):
        return numeric(value)
    if value.get("kind") != "expression/1":
        raise Unsupported("Opaque output expressions cannot be imported; use a bound export")

    def tree(node):
        op, *args = node
        if op == "parameter":
            return parameters[args[0]]
        if op == "integer":
            return args[0]
        if op == "rational":
            return Fraction(args[0], args[1])
        if op == "float":
            return numeric(args[0])
        if op == "pi":
            return math.pi
        values = [tree(arg) for arg in args]
        if op == "add":
            rational = sum((v for v in values if isinstance(v, Fraction)), Fraction())
            result = sum(v for v in values if not isinstance(v, Fraction))
            return (result * rational.denominator + rational.numerator) / rational.denominator
        if op == "mul":
            result = 1
            rational = Fraction(1)
            for v in values:
                if isinstance(v, Fraction):
                    rational *= v
                else:
                    result *= v
            return result * rational.numerator / rational.denominator
        if op == "pow":
            return values[0] ** values[1]
        if op in {"sin", "cos", "tan", "exp", "log", "abs", "arcsin", "arccos", "arctan"}:
            if hasattr(values[0], op):
                return getattr(values[0], op)()
            return (
                abs(values[0])
                if op == "abs"
                else getattr(
                    math, {"arcsin": "asin", "arccos": "acos", "arctan": "atan"}.get(op, op)
                )(values[0])
            )
        raise Unsupported(f"Unsupported expression operation {op}")

    return tree(value["tree"])


def _matrix(matrix):
    return [[[number(complex(v).real), number(complex(v).imag)] for v in row] for row in matrix]


def operation_descriptor(op, parameters, opaque=False):
    from qiskit.circuit import AnnotatedOperation, ControlledGate
    from qiskit.circuit.controlflow import ControlFlowOp
    from qiskit.circuit.library import (
        MCMTGate,
        PauliEvolutionGate,
        UnitaryGate,
    )

    standard = standard_gates()
    name = op.name
    payload = None
    params = []
    if isinstance(op, UnitaryGate):
        payload = {"kind": "matrix/1", "arity": op.num_qubits, "matrix": _matrix(op.to_matrix())}
    elif isinstance(op, PauliEvolutionGate):
        from qiskit.quantum_info import SparseObservable, SparsePauliOp

        operators = op.operator if isinstance(op.operator, list) else [op.operator]
        groups = []
        for operator in operators:
            if isinstance(operator, SparseObservable):
                operator = SparsePauliOp.from_sparse_observable(operator)
            groups.append(
                [
                    [pauli, number(complex(coef).real), number(complex(coef).imag)]
                    for pauli, coef in operator.to_list()
                ]
            )
        synthesis = op.synthesis
        if type(synthesis).__name__ not in {"LieTrotter", "SuzukiTrotter"}:
            raise Unsupported("Unsupported evolution synthesis")
        try:
            settings = synthesis.settings
        except Exception as exc:
            raise Unsupported("Custom evolution callbacks are unsupported") from exc
        settings = dict(settings, preserve_order=getattr(synthesis, "preserve_order", True))
        payload = {
            "kind": "pauli_evolution/1",
            "width": op.num_qubits,
            "groups": groups,
            "grouped": isinstance(op.operator, list),
            "synthesis": {"class": type(synthesis).__name__, "settings": settings},
        }
        params = [encode_value(op.time, parameters, opaque)]
    elif isinstance(op, AnnotatedOperation):
        modifiers = []
        for mod in op.modifiers:
            kind = type(mod).__name__
            if kind == "InverseModifier":
                modifiers.append({"kind": "inverse"})
            elif kind == "ControlModifier":
                modifiers.append(
                    {
                        "kind": "control",
                        "num_ctrl_qubits": mod.num_ctrl_qubits,
                        "ctrl_state": mod.ctrl_state,
                    }
                )
            elif kind == "PowerModifier":
                modifiers.append({"kind": "power", "power": encode_value(mod.power, parameters)})
            else:
                raise Unsupported(f"Unsupported annotated modifier: {kind}")
        payload = {
            "kind": "annotated/1",
            "base": operation_descriptor(op.base_op, parameters),
            "modifiers": modifiers,
        }
    elif isinstance(op, MCMTGate):
        payload = {
            "kind": "mcmt/1",
            "base": operation_descriptor(op.base_gate, parameters),
            "num_ctrl_qubits": op.num_ctrl_qubits,
            "num_target_qubits": op.num_target_qubits,
            "ctrl_state": op.ctrl_state,
        }
    elif type(op).__name__ in {
        "HalfAdderGate",
        "FullAdderGate",
        "ModularAdderGate",
        "MultiplierGate",
    }:
        fields = {"num_state_qubits": op.num_state_qubits}
        if type(op).__name__ == "MultiplierGate":
            fields["num_result_qubits"] = op.num_result_qubits
        payload = {"kind": "library/1", "class": type(op).__name__, "arguments": fields}
    elif isinstance(op, ControlFlowOp):
        # Conditions need the enclosing circuit's bit indices, attached by export_circuit.
        payload = {
            "kind": "control_flow/1",
            "blocks": [export_circuit(b, opaque=opaque) for b in op.blocks],
        }
        if name == "for_loop":
            indices, parameter, _body = op.params
            if parameter is not None:
                raise Unsupported(
                    "Parameterized loop bodies require explicit unrolling at curation"
                )
            payload["indices"] = list(indices)
        elif name not in {"if_else", "while_loop"}:
            raise Unsupported(f"Unsupported control flow {name}")
    elif name == "barrier":
        pass
    elif name == "delay":
        params = [encode_value(op.duration, parameters, opaque)]
        payload = {"kind": "delay/1", "unit": op.unit}
    elif (
        name in standard
        and op.base_class is standard[name].base_class
        and not (isinstance(op, ControlledGate) and op.ctrl_state != 2**op.num_ctrl_qubits - 1)
    ):
        params = [encode_value(p, parameters, opaque) for p in op.params]
    else:
        raise Unsupported(f"No typed constructor for {type(op).__name__} ({name})")
    return {
        "name": name,
        "arity": op.num_qubits,
        "clbits": op.num_clbits,
        "parameters": params,
        "payload": payload,
    }


def construct_operation(desc, parameters):
    from qiskit.circuit import (
        AnnotatedOperation,
        Barrier,
        ControlModifier,
        Delay,
        InverseModifier,
        PowerModifier,
    )
    from qiskit.circuit.library import (
        MCMTGate,
        PauliEvolutionGate,
        UnitaryGate,
    )

    name, payload = desc["name"], desc["payload"]
    params = [decode_value(p, parameters) for p in desc["parameters"]]
    if payload is None:
        if name == "barrier":
            return Barrier(desc["arity"])
        standard = standard_gates()
        if name not in standard:
            raise Unsupported(f"Unknown standard operation {name}")
        template = standard[name]
        if desc["arity"] != template.num_qubits or desc["clbits"] != template.num_clbits:
            raise Unsupported(f"Arity mismatch for {name}")
        return template.base_class(*params)
    kind = payload["kind"]
    if kind == "delay/1":
        return Delay(params[0], unit=payload["unit"])
    if kind == "matrix/1":
        matrix = [
            [complex(numeric(re), numeric(im)) for re, im in row] for row in payload["matrix"]
        ]
        return UnitaryGate(matrix)
    if kind == "pauli_evolution/1":
        from qiskit.quantum_info import SparsePauliOp
        from qiskit.synthesis import LieTrotter, SuzukiTrotter

        groups = [
            SparsePauliOp.from_list([(p, complex(numeric(re), numeric(im))) for p, re, im in group])
            for group in payload["groups"]
        ]
        synth = {"LieTrotter": LieTrotter, "SuzukiTrotter": SuzukiTrotter}.get(
            payload["synthesis"]["class"]
        )
        if synth is None:
            raise Unsupported("Unknown synthesis class")
        return PauliEvolutionGate(
            groups if payload["grouped"] else groups[0],
            time=params[0],
            synthesis=synth(**payload["synthesis"]["settings"]),
        )
    if kind == "annotated/1":
        modifiers = []
        for mod in payload["modifiers"]:
            if mod["kind"] == "inverse":
                modifiers.append(InverseModifier())
            elif mod["kind"] == "control":
                modifiers.append(ControlModifier(mod["num_ctrl_qubits"], mod["ctrl_state"]))
            elif mod["kind"] == "power":
                modifiers.append(PowerModifier(decode_value(mod["power"], parameters)))
            else:
                raise Unsupported("Unknown modifier")
        return AnnotatedOperation(construct_operation(payload["base"], parameters), modifiers)
    if kind == "mcmt/1":
        return MCMTGate(
            construct_operation(payload["base"], parameters),
            payload["num_ctrl_qubits"],
            payload["num_target_qubits"],
            ctrl_state=payload["ctrl_state"],
        )
    if kind == "library/1":
        from qiskit.circuit.library import (
            FullAdderGate,
            HalfAdderGate,
            ModularAdderGate,
            MultiplierGate,
        )

        constructors = {
            c.__name__: c for c in (FullAdderGate, HalfAdderGate, ModularAdderGate, MultiplierGate)
        }
        if payload["class"] not in constructors:
            raise Unsupported("Unknown typed library constructor")
        return constructors[payload["class"]](**payload["arguments"])
    raise Unsupported(f"Unknown payload kind: {kind}")


def export_circuit(circuit, opaque=False, stream=False):
    parameters = {
        p: f"p{i}" for i, p in enumerate(sorted(circuit.parameters, key=lambda p: p.name))
    }
    if len({p.name for p in parameters}) != len(parameters):
        raise Unsupported("Duplicate parameter names")
    header = {
        "format": CIRCUIT_FORMAT,
        "num_qubits": circuit.num_qubits,
        "num_clbits": circuit.num_clbits,
        "qregs": [[r.name, [circuit.find_bit(b).index for b in r]] for r in circuit.qregs],
        "cregs": [[r.name, [circuit.find_bit(b).index for b in r]] for r in circuit.cregs],
        "global_phase": encode_value(circuit.global_phase, parameters, opaque),
        "parameters": [{"id": id_, "name": p.name} for p, id_ in parameters.items()],
    }
    operations = export_operations(circuit, parameters, opaque)
    return {"header": header, "operations": operations if stream else list(operations)}


def export_operations(circuit, parameters, opaque=False):
    for inst in circuit.data:
        desc = operation_descriptor(inst.operation, parameters, opaque)
        qs = [circuit.find_bit(q).index for q in inst.qubits]
        cs = [circuit.find_bit(c).index for c in inst.clbits]
        payload = desc["payload"]
        if payload and payload["kind"] == "control_flow/1" and inst.operation.name != "for_loop":
            condition = inst.operation.condition
            if not isinstance(condition, tuple):
                raise Unsupported("Only bit/register equality conditions are supported")
            bits, value = condition
            try:
                indices = [circuit.find_bit(bit).index for bit in bits]
            except TypeError:
                indices = [circuit.find_bit(bits).index]
            payload["condition"] = {"bits": indices, "value": int(value)}
        yield [desc["name"], qs, cs, desc["parameters"], payload]


def import_circuit(data):
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
    from qiskit.circuit import Clbit, Parameter, Qubit
    from qiskit.circuit.controlflow import ForLoopOp, IfElseOp, WhileLoopOp

    header, operations = data["header"], data["operations"]
    if header["format"] != CIRCUIT_FORMAT:
        raise Unsupported("Unknown circuit format")
    circuit = QuantumCircuit()
    circuit.add_bits([Qubit() for _ in range(header["num_qubits"])])
    circuit.add_bits([Clbit() for _ in range(header["num_clbits"])])
    for field, cls, bits in (
        ("qregs", QuantumRegister, circuit.qubits),
        ("cregs", ClassicalRegister, circuit.clbits),
    ):
        cursor = 0
        for name, indices in header[field]:
            if isinstance(indices, int):
                indices = list(range(cursor, cursor + indices))
                cursor += len(indices)
            circuit.add_register(cls(name=name, bits=[bits[i] for i in indices]))
    parameters = {p["id"]: Parameter(p["name"]) for p in header["parameters"]}
    circuit.global_phase = decode_value(header["global_phase"], parameters)
    for name, qs, cs, params, payload in operations:
        if payload and payload["kind"] == "control_flow/1":
            blocks = [import_circuit(block) for block in payload["blocks"]]
            if name == "for_loop":
                op = ForLoopOp(payload["indices"], None, blocks[0])
            else:
                indices = payload["condition"]["bits"]
                if len(indices) == 1:
                    bits = circuit.clbits[indices[0]]
                else:
                    bits = next(
                        (
                            r
                            for r in circuit.cregs
                            if [circuit.find_bit(b).index for b in r] == indices
                        ),
                        None,
                    )
                    if bits is None:
                        raise Unsupported("Condition register is not declared")
                condition = bits, payload["condition"]["value"]
                if name == "if_else":
                    op = IfElseOp(condition, blocks[0], blocks[1] if len(blocks) > 1 else None)
                elif name == "while_loop":
                    op = WhileLoopOp(condition, blocks[0])
                else:
                    raise Unsupported("Unknown control-flow operation")
        else:
            op = construct_operation(
                {
                    "name": name,
                    "arity": len(qs),
                    "clbits": len(cs),
                    "parameters": params,
                    "payload": payload,
                },
                parameters,
            )
        circuit.append(op, qs, cs)
    return circuit


def export_layout(circuit, input_width):
    layout = circuit.layout
    if layout is None:
        return None
    return {
        "input_num_qubits": input_width,
        "output_num_qubits": circuit.num_qubits,
        "initial_index_layout": layout.initial_index_layout(filter_ancillas=False),
        "final_index_layout": layout.final_index_layout(filter_ancillas=False),
        "routing_permutation": layout.routing_permutation(),
    }


def export_target(target, native_2q_names):
    from qiskit.circuit import ParameterExpression

    instructions = []
    for name in target.operation_names:
        op = target.operation_from_name(name)
        if isinstance(op, type):
            instructions.append(
                {"name": name, "arity": None, "parameters": [], "qargs": None, "properties": []}
            )
            continue
        qargs, properties = [], []
        for qubits, prop in target[name].items():
            if qubits is None:
                qargs = None
            elif qargs is not None:
                qargs.append(list(qubits))
            properties.append(
                {
                    "qargs": None if qubits is None else list(qubits),
                    "duration": None
                    if prop is None or prop.duration is None
                    else number(prop.duration),
                    "error": None if prop is None or prop.error is None else number(prop.error),
                }
            )
        spec = {
            "name": name,
            "arity": op.num_qubits,
            "parameters": [
                str(p) if isinstance(p, ParameterExpression) else f"p{i}"
                for i, p in enumerate(op.params)
            ],
            "qargs": qargs,
            "properties": properties,
        }
        fixed = {
            str(i): number(p)
            for i, p in enumerate(op.params)
            if not isinstance(p, ParameterExpression)
        }
        if fixed:
            spec["fixed_parameters"] = fixed
        if target.gate_has_angle_bounds(name):
            # Qiskit 2.5 has public bound validation but no public bound getter.
            # This version adapter reads only the numeric field of its state;
            # an unknown state layout is refused, never inferred by probing.
            try:
                bounds = target.__getstate__()["base"]["gate_map"][name]["angle_bounds"]
                if len(bounds) != len(op.params):
                    raise ValueError("Wrong angle-bound arity")
                spec["angle_bounds"] = [
                    None if pair is None else [number(x) for x in pair] for pair in bounds
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise Unsupported("Unknown target angle-bound representation") from exc
        instructions.append(spec)
    result = {
        "format": TARGET_FORMAT,
        "num_qubits": target.num_qubits,
        "dt": None if target.dt is None else number(target.dt),
        "timing_constraints": {
            k: getattr(target, k)
            for k in ("granularity", "min_length", "pulse_alignment", "acquire_alignment")
        },
        "instructions": instructions,
        "native_2q_names": list(native_2q_names),
    }
    if target.qubit_properties is not None:
        result["qubit_properties"] = [
            {
                k: None if getattr(q, k) is None else number(getattr(q, k))
                for k in ("t1", "t2", "frequency")
            }
            for q in target.qubit_properties
        ]
    return result


def import_target(data):
    from qiskit.circuit import Parameter
    from qiskit.circuit.controlflow import ForLoopOp, IfElseOp, WhileLoopOp
    from qiskit.providers import QubitProperties
    from qiskit.transpiler import InstructionProperties, Target

    if data["format"] != TARGET_FORMAT:
        raise Unsupported("Unknown target format")
    qubit_properties = data.get("qubit_properties")
    target = Target(
        num_qubits=data["num_qubits"],
        dt=None if data["dt"] is None else numeric(data["dt"]),
        qubit_properties=None
        if qubit_properties is None
        else [
            QubitProperties(**{k: None if v is None else numeric(v) for k, v in q.items()})
            for q in qubit_properties
        ],
        **data["timing_constraints"],
    )
    standard = standard_gates()
    control_flow = {"if_else": IfElseOp, "while_loop": WhileLoopOp, "for_loop": ForLoopOp}
    for spec in data["instructions"]:
        name = spec["name"]
        if spec["arity"] is None:
            if name not in control_flow:
                # GenericBackend also provides break/continue/switch instructions.
                from qiskit.circuit.controlflow import (
                    BoxOp,
                    BreakLoopOp,
                    ContinueLoopOp,
                    SwitchCaseOp,
                )

                control_flow.update(
                    break_=BreakLoopOp, continue_=ContinueLoopOp, switch_case=SwitchCaseOp
                )
                control_flow.update(
                    {"break": BreakLoopOp, "continue": ContinueLoopOp, "box": BoxOp}
                )
            if name not in control_flow:
                raise Unsupported(f"Unknown variable-width target operation {name}")
            target.add_instruction(control_flow[name], name=name)
            continue
        if name not in standard:
            raise Unsupported(f"Unknown target instruction {name}")
        params = [
            numeric(spec["fixed_parameters"][str(i)])
            if str(i) in spec.get("fixed_parameters", {})
            else Parameter(p)
            for i, p in enumerate(spec["parameters"])
        ]
        op = standard[name].base_class(*params)
        properties = {
            None if p["qargs"] is None else tuple(p["qargs"]): None
            if p["duration"] is None and p["error"] is None
            else InstructionProperties(
                duration=None if p["duration"] is None else numeric(p["duration"]),
                error=None if p["error"] is None else numeric(p["error"]),
            )
            for p in spec["properties"]
        }
        kwargs = {}
        if spec.get("angle_bounds"):
            kwargs["angle_bounds"] = [
                None if pair is None else tuple(map(numeric, pair)) for pair in spec["angle_bounds"]
            ]
        target.add_instruction(op, properties=properties or None, name=name, **kwargs)
    return target


def compile_options(case, target, seed):
    options = dict(case["options"], seed_transpiler=seed)
    if case["optimization_level"] is not None:
        options["optimization_level"] = case["optimization_level"]
    if case["constraint_form"] == "target":
        options["target"] = target
    else:
        options.update(basis_gates=case["basis_gates"], coupling_map=case["coupling_map"])
    return options


def preset(case, target, seed, edits=()):
    from qiskit.transpiler import CouplingMap
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    options = compile_options(case, target, seed)
    if "coupling_map" in options and options["coupling_map"] is not None:
        options["coupling_map"] = CouplingMap(options["coupling_map"])
    for edit in edits:
        if edit == "unitary_synthesis_method=clifford":
            options["unitary_synthesis_method"] = "clifford"
        elif not edit.startswith("drop_stage:"):
            raise Unsupported(f"Unknown pipeline edit {edit}")
    pm = generate_preset_pass_manager(**options)
    for edit in edits:
        if edit.startswith("drop_stage:"):
            stage = edit.split(":", 1)[1]
            if stage not in pm.stages:
                raise Unsupported(f"Unknown pipeline stage {stage}")
            for attr in (stage, f"pre_{stage}", f"post_{stage}"):
                setattr(pm, attr, None)
    return pm


def fingerprint(pm):
    budget = (
        "layout_trials",
        "swap_trials",
        "max_iterations",
        "trials",
        "heuristic",
        "call_limit",
        "max_trials",
    )

    def walk(controller):
        passes = getattr(controller, "tasks", None)
        if passes is None:
            return [
                {
                    "pass": type(controller).__name__,
                    "budget": {key: getattr(controller, key, "unknown") for key in budget},
                }
            ]
        result = []
        for task in passes:
            result.extend(walk(task))
        return result

    return {
        stage: walk(getattr(pm, stage).to_flow_controller()) if getattr(pm, stage) else []
        for stage in pm.expanded_stages
    }
