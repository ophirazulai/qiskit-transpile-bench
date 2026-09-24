from qtb.canonical import read_json
from qtb.config import data_root, load_profile


def test_t1_t2_metadata_matches_frozen_fixture_provenance():
    provenance = {
        row["input_group"]: row
        for row in read_json(data_root() / "fixtures/provenance.json")
    }
    source = "test/benchmarks/transpiler_benchmarks.py"
    for profile in ("iterations-profile", "confirm-profile"):
        manifest, policy, _ = load_profile(profile)
        assert manifest["version"] == policy["version"] == 2
        for case_id, name in (("T1", "single_h"), ("T2", "cancel_2q")):
            case = next(case for case in manifest["cases"] if case["case_id"] == case_id)
            fixture = provenance[name]
            assert case["input_group"] == name
            assert case["circuit"] == fixture["artifact"]
            assert case["provenance"] == {
                "commit": fixture["commit"], "grade": "A", "source": source,
            }
            assert case["family"] == "timing_microbenchmark"
            assert case["topology"] == "heavy_hex"
            assert case["target"]["file"] == "targets/mumbai_27_loose.target.json"
            assert fixture["source"] == source
