from types import SimpleNamespace

import pytest
from qiskit import QuantumCircuit

from qtb.coordinator.checks import clifford_checks
from qtb.evaluator import record, verdict
from qtb_verifier.small_exact import verify_clifford


@pytest.mark.parametrize(
    "mutation, expected_status, expected_verdict",
    [("drop_cz", "mismatch", "CONSTRAINT_VIOLATION"),
     ("perturb_rz", "unverified", "INCONCLUSIVE")],
)
def test_c7_mutations_propagate_to_verdict(tmp_path, mutation, expected_status, expected_verdict):
    reference = QuantumCircuit(2)
    reference.h(0)
    reference.cz(0, 1)
    corrupted = reference.copy()
    if mutation == "drop_cz":
        corrupted.data.pop()
    else:
        corrupted.rz(0.001, 0)
    assert verify_clifford(reference, corrupted, None)["status"] == expected_status

    evidence = []

    def job(revision, variant, mode, seeds, edits):
        return [
            {"status": "ok", "seed": seed,
             "corrupted": revision == "evolved" and bool(edits) and seed == 0}
            for seed in seeds
        ]

    comparison = SimpleNamespace(
        directory=tmp_path,
        prefix="IA",
        jobs=lambda specs: [job(*spec) for spec in specs],
        verify_many=lambda requests: [
            verify_clifford(
                reference, corrupted if output["corrupted"] else reference.copy(), None
            )
            for _case, output, *_rest in requests
        ],
        evidence=evidence.append,
    )
    case = {"case_id": "sample", "clifford_variant": {"sha256": "fixture"}, "options": {}}
    clifford_checks(comparison, [case])
    aggregate = next(row for row in evidence if row["id"] == "IA1/C7")
    assert aggregate["result"] == "unresolved"
    decision = verdict(
        [*evidence, record("IA2/improvement", "improvement", "passed")],
        ["IA1/C7", "IA2/improvement"],
    )
    assert decision == expected_verdict
