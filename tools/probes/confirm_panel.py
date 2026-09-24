"""The `confirm-profile` panel: roles, splits, weights, cost and the draft manifest.

Behind section 3.7 of design/transpilation-benchmark-impl-plan.md.  Reads the raw
probe records of confirm_probe.jsonl (produced by confirm_probe.py) and
prints the coverage, weight and cost summary; with --manifest it also rewrites
design/confirm-profile.draft-manifest.json, and with --tables the markdown tables of the
plan.  Run from anywhere:  python design/probes/confirm_panel.py [--manifest] [--tables]
"""
import collections
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "confirm_probe.jsonl")
MANIFEST = os.path.join(HERE, "..", "confirm-profile.draft-manifest.json")

# ----------------------------------------------------------------------------- panel
# probe case id -> (split, {level: role})
# roles: S scored, G guard (SE-based, whole block), D deterministic guard, Z zero-baseline
# guard, C canary, H held-out (20 seeds), T per-case guard (10 seeds)
def _lv(spec):
    out = {}
    for part in spec.split():
        role, levels = part.split(":")
        for l in levels:
            out[int(l)] = role
    return out


PANEL = {
    # G1 QFT
    "qft_n100": ("tuning", _lv("S:0123")),
    "qftbm_14": ("tuning", _lv("S:0123")),
    "ft_qft_32": ("tuning", _lv("S:0123")),
    "qftbm_8": ("validation", _lv("S:0123")),
    "ft_qft_64": ("validation", _lv("S:0123")),
    "time_qft_16": ("guard", _lv("G:01 Z:23")),
    # G2 Hamiltonian simulation
    "square_heisenberg_n100": ("tuning", _lv("S:0123")),
    "ft_trotter_32": ("tuning", _lv("S:0 D:123")),
    "ft_trotter_16": ("validation", _lv("S:0 D:123")),
    "dtc_n100": ("validation", _lv("S:0 D:123")),
    # G3 QAOA
    "qaoa_ba_n100_3reps": ("tuning", _lv("S:0123")),
    "ft_qaoa_8": ("tuning", _lv("S:0123")),
    "ft_qaoa_16": ("validation", _lv("S:0123")),
    "ft_qaoa_32": ("validation", _lv("S:0123")),
    # G4 quantum volume
    "qv_n50_d50": ("tuning", _lv("S:0123")),
    "qv_14_x_14": ("tuning", _lv("S:0123")),
    "qv_w27": ("tuning", _lv("S:0123")),
    "qv_50_x_20": ("validation", _lv("S:0123")),
    "qv_w14": ("validation", _lv("S:0123")),
    # G5 reversible logic
    "ripple_10": ("tuning", _lv("S:0123")),
    "depth_mod8_10_178": ("tuning", _lv("S:0123")),
    "depth_4gt10_v1_81": ("tuning", _lv("S:0123")),
    "ft_multiplier_32": ("tuning", _lv("S:0123")),
    "ft_mcx_16": ("tuning", _lv("S:0123")),
    "ripple_20": ("validation", _lv("S:0123")),
    "time_cnt3_5_179": ("validation", _lv("S:0123")),
    "time_cnt3_5_180": ("validation", _lv("S:0123")),
    "ft_modular_adder_32": ("validation", _lv("S:0123")),
    "ft_multiplier_16": ("validation", _lv("S:0123")),
    "depth_4mod5_v0_19": ("guard", _lv("G:0123")),
    "hwb12": ("guard", _lv("H:2")),
    # G6 Bernstein-Vazirani
    "bv_all_ones_n100": ("tuning", _lv("S:0123")),
    "bv_all_ones_n16": ("tuning", _lv("S:0123")),
    "bv_all_ones_n50": ("validation", _lv("S:0123")),
    "bvlike_n100": ("guard", _lv("G:01 Z:23")),
    # G7 variational ansatz
    "su2_circ_89": ("tuning", _lv("S:012 T:3")),
    "ring_8": ("tuning", _lv("S:0 D:123")),
    "ring_14": ("validation", _lv("S:0 D:123")),
    "su2_circ_100": ("validation", _lv("S:0 C:123")),
    # G8 routing challenges
    "queko_bss_53_sabre": ("tuning", _lv("S:0123")),
    "queko_bigd_20_sabre": ("tuning", _lv("S:0123")),
    "queko_bntf_54_sabre": ("validation", _lv("S:0123")),
    "queko_bss_53": ("guard", _lv("G:0 D:123")),
    "queko_bigd_20": ("guard", _lv("G:0 D:123")),
    "queko_bntf_54": ("guard", _lv("G:0 D:123")),
    # canary
    "long_2q_sequence": ("guard", _lv("C:23")),
    # all-to-all control (transpiler_ft.py configuration)
    "ft_a2a_qft_16": ("guard", _lv("D:0123")),
    "ft_a2a_trotter_16": ("guard", _lv("D:0123")),
    "ft_a2a_qaoa_16": ("guard", _lv("D:0123")),
    "ft_a2a_multiplier_16": ("guard", _lv("D:0123")),
    "ft_a2a_modular_adder_16": ("guard", _lv("D:0123")),
    "ft_a2a_mcx_16": ("guard", _lv("D:0123")),
}
SEEDS = {"S": 100, "G": 100, "D": 10, "Z": 10, "C": 10, "H": 20, "T": 10}
ROLE_NAME = {"S": "scored", "G": "guard", "D": "deterministic", "Z": "zero_baseline", "C": "canary", "H": "held_out", "T": "guard"}
ROLE_TEXT = {"S": "scored", "G": "guard", "D": "deterministic", "Z": "zero-baseline", "C": "canary", "H": "held-out (20 seeds)", "T": "guard (10 seeds)"}

# probe id -> manifest input id
NAME = {
    "qft_n100": "qft_n100", "qftbm_14": "qft_cp_n14", "qftbm_8": "qft_cp_n8", "ft_qft_32": "qft_full_n32",
    "ft_qft_64": "qft_full_n64", "time_qft_16": "qft16_cancel", "square_heisenberg_n100": "square_heisenberg_n100",
    "ft_trotter_32": "trotter_chain_n32", "ft_trotter_16": "trotter_chain_n16", "dtc_n100": "dtc_n100",
    "qaoa_ba_n100_3reps": "qaoa_ba_n100_3reps", "ft_qaoa_8": "qaoa_complete_n8", "ft_qaoa_16": "qaoa_complete_n16",
    "ft_qaoa_32": "qaoa_complete_n32", "qv_n50_d50": "qv_n50_d50", "qv_14_x_14": "qv_n14_d14", "qv_w27": "qv_n27_d27",
    "qv_50_x_20": "qv_n50_d20", "qv_w14": "qv_n14_d14_s10", "ripple_10": "ripple_adder_10", "ripple_20": "ripple_adder_20",
    "depth_mod8_10_178": "revlib_mod8_10_178", "depth_4gt10_v1_81": "revlib_4gt10_v1_81",
    "depth_4mod5_v0_19": "revlib_4mod5_v0_19", "time_cnt3_5_179": "revlib_cnt3_5_179",
    "time_cnt3_5_180": "revlib_cnt3_5_180", "ft_multiplier_32": "multiplier_h18_n32",
    "ft_multiplier_16": "multiplier_h18_n16", "ft_mcx_16": "mcx_kg24_n16", "ft_modular_adder_32": "adder_modular_v17_n32",
    "hwb12": "hwb12", "bv_all_ones_n100": "bv_all_ones_n100", "bv_all_ones_n16": "bv_all_ones_n16",
    "bv_all_ones_n50": "bv_all_ones_n50", "bvlike_n100": "bvlike_n100", "su2_circ_89": "su2_circular_n89",
    "su2_circ_100": "su2_circular_n100", "ring_8": "ring_random_n8", "ring_14": "ring_random_n14",
    "queko_bss_53_sabre": "queko_bss_53", "queko_bigd_20_sabre": "queko_bigd_20", "queko_bntf_54_sabre": "queko_bntf_54",
    "queko_bss_53": "queko_bss_53", "queko_bigd_20": "queko_bigd_20", "queko_bntf_54": "queko_bntf_54",
    "long_2q_sequence": "long_2q_sequence", "ft_a2a_qft_16": "a2a_qft_n16", "ft_a2a_trotter_16": "a2a_trotter_n16",
    "ft_a2a_qaoa_16": "a2a_qaoa_n16", "ft_a2a_multiplier_16": "a2a_multiplier_n16",
    "ft_a2a_modular_adder_16": "a2a_adder_modular_n16", "ft_a2a_mcx_16": "a2a_mcx_n16",
}
SOURCE = {
    "qft_n100": "utility_scale.py", "qftbm_14": "qft.py", "qftbm_8": "qft.py", "ft_qft_32": "utils.qft_circuit",
    "ft_qft_64": "utils.qft_circuit", "time_qft_16": "transpiler_qualitative.py",
    "square_heisenberg_n100": "utility_scale.py", "ft_trotter_32": "utils.trotter_circuit",
    "ft_trotter_16": "utils.trotter_circuit", "dtc_n100": "qasm/dtc_100_cx_12345.qasm",
    "qaoa_ba_n100_3reps": "utility_scale.py", "ft_qaoa_8": "utils.qaoa_circuit", "ft_qaoa_16": "utils.qaoa_circuit",
    "ft_qaoa_32": "utils.qaoa_circuit", "qv_n50_d50": "utility_scale.py", "qv_14_x_14": "transpiler_levels.py",
    "qv_w27": "quantum_volume.py", "qv_50_x_20": "transpiler_levels.py", "qv_w14": "quantum_volume.py",
    "ripple_10": "ripple_adder.py", "ripple_20": "ripple_adder.py", "depth_mod8_10_178": "transpiler_qualitative.py",
    "depth_4gt10_v1_81": "transpiler_qualitative.py", "depth_4mod5_v0_19": "transpiler_qualitative.py",
    "time_cnt3_5_179": "transpiler_qualitative.py", "time_cnt3_5_180": "transpiler_qualitative.py",
    "ft_multiplier_32": "utils.multiplier_circuit", "ft_multiplier_16": "utils.multiplier_circuit",
    "ft_mcx_16": "utils.mcx_circuit", "ft_modular_adder_32": "utils.modular_adder_circuit", "hwb12": "utility_scale.py",
    "bv_all_ones_n100": "utility_scale.py", "bv_all_ones_n16": "utils.bv_all_ones", "bv_all_ones_n50": "utils.bv_all_ones",
    "bvlike_n100": "utility_scale.py", "su2_circ_89": "utility_scale.py", "su2_circ_100": "utility_scale.py",
    "ring_8": "random_circuit_hex.py", "ring_14": "random_circuit_hex.py", "queko_bss_53_sabre": "queko.py",
    "queko_bigd_20_sabre": "queko.py", "queko_bntf_54_sabre": "queko.py", "queko_bss_53": "queko.py",
    "queko_bigd_20": "queko.py", "queko_bntf_54": "queko.py", "long_2q_sequence": "transpiler_levels.py",
}
# probe target id -> (manifest id, topology class, native 2q basis, recipe)
TARGETS = {
    "hh9_cz": ("heavy_hex_d9_cz", "heavy_hex", "cz", "GenericBackendV2(193, ['rz','x','sx','cz','id'], coupling_map=CouplingMap.from_heavy_hex(9), control_flow=True, seed=12345678942)"),
    "hh9_cx": ("heavy_hex_d9_cx", "heavy_hex", "cx", "as heavy_hex_d9_cz with 'cx'"),
    "mumbai27": ("mumbai_27", "heavy_hex", "cx", "GenericBackendV2(num_qubits=27, coupling_map=legacy_cmaps.MUMBAI_CMAP, seed=0)"),
    "mumbai27_loose": ("mumbai_27_loose", "heavy_hex", "cx", "coupling_map=MUMBAI_CMAP, basis_gates=['id','rz','sx','x','cx','reset'] (quantum_volume.py)"),
    "rochester53_sx": ("rochester_53", "heavy_hex", "cx", "coupling_map=<queko.py rochester list>, basis_gates=['id','rz','sx','x','cx']"),
    "rochester53_u": ("rochester_53_u", "heavy_hex", "cx", "coupling_map=<transpiler_levels.py rochester list>, basis_gates=['u1','u2','u3','cx','id']"),
    "melbourne14": ("melbourne_14", "grid", "cx", "GenericBackendV2(num_qubits=14, coupling_map=legacy_cmaps.MELBOURNE_CMAP, seed=0)  # directed"),
    "melbourne14_u": ("melbourne_14_u", "grid", "cx", "coupling_map=MELBOURNE_CMAP (directed), basis_gates=['u1','u2','u3','cx','id']"),
    "tokyo20": ("tokyo_20", "grid", "cx", "coupling_map=<queko.py tokyo list>, basis_gates=['id','rz','sx','x','cx']"),
    "sycamore54": ("sycamore_54", "grid", "cx", "coupling_map=<queko.py sycamore list>, basis_gates=['id','rz','sx','x','cx']"),
    "grid5": ("grid_5x5_u", "grid", "cx", "coupling_map=CouplingMap.from_grid(5, 5), basis_gates=['u1','u2','u3','cx','id']"),
    "grid7": ("grid_7x7_u", "grid", "cx", "coupling_map=CouplingMap.from_grid(7, 7), basis_gates=['u1','u2','u3','cx','id']"),
    "ft_a2a_16": ("a2a_clifford_rz_16", "all_to_all", "clifford_2q", "Target.from_configuration(['rz','measure'] + get_clifford_gate_names(), 16)"),
}
DIRECTED = {"melbourne14", "melbourne14_u"}
# declared registers wider than the qubits that carry gates (measured at the baseline)
ACTIVE = {"depth_4gt10_v1_81": 5, "depth_4mod5_v0_19": 5, "depth_mod8_10_178": 6, "queko_bigd_20": 9, "queko_bigd_20_sabre": 9}
TIMING_INPUTS = ["qft_n100", "square_heisenberg_n100", "qaoa_ba_n100_3reps", "qv_n50_d50", "ft_multiplier_32",
                 "bv_all_ones_n100", "queko_bss_53_sabre", "su2_circ_89"]


# ----------------------------------------------------------------------------- data
def load():
    """Fold the raw probe records into {case: {"facts": ..., "levels": {level: ...}}}."""
    facts, comp = {}, collections.defaultdict(list)
    for line in open(PROBE):
        r = json.loads(line)
        if r["kind"] == "facts":
            facts[r["case"]] = r
        elif r.get("ok"):
            comp[(r["case"], r["level"])].append(r)
    out = {}
    for c, f in facts.items():
        levels = {}
        for l in range(4):
            rs = comp.get((c, l), [])
            if not rs:
                continue
            d2 = [r["D2"] for r in rs]
            n2 = [r["N2"] for r in rs]
            t = sorted(r["t"] for r in rs)
            sens = None if len(rs) == 1 else (len(set(d2)) > 1 or len(set(n2)) > 1)
            sd = 0.0
            if min(d2) > 0 and len(d2) > 1:
                ls = [math.log(x) for x in d2]
                m = sum(ls) / len(ls)
                sd = math.sqrt(sum((x - m) ** 2 for x in ls) / (len(ls) - 1))
            levels[l] = {"t": t[len(t) // 2], "D2": d2, "N2": n2, "seed_sensitive": sens, "sd_lnD2": sd}
        out[c] = {"facts": f, "levels": levels}
    return out


def band(active):
    return "small" if active <= 16 else ("medium" if active <= 64 else "large")


def active_qubits(c, facts):
    return ACTIVE.get(c, facts["n_qubits"])


# ----------------------------------------------------------------------------- weights
def weights(S, split):
    """Family -> level -> band -> topology -> basis -> group; only populated children share."""
    tree = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(
        lambda: collections.defaultdict(lambda: collections.defaultdict(set)))))
    for c, (sp, roles) in PANEL.items():
        if sp != split:
            continue
        f = S[c]["facts"]
        _, topo, basis, _ = TARGETS[f["target"]]
        for l, r in roles.items():
            if r == "S":
                tree[f["family"]][l][band(active_qubits(c, f))][topo][basis].add(c)
    w = {}
    for fam in tree:
        wf = 1 / len(tree)
        for l in tree[fam]:
            wl = wf / len(tree[fam])
            for b in tree[fam][l]:
                wb = wl / len(tree[fam][l])
                for topo in tree[fam][l][b]:
                    wt = wb / len(tree[fam][l][b])
                    for basis in tree[fam][l][b][topo]:
                        wbs = wt / len(tree[fam][l][b][topo])
                        for c in tree[fam][l][b][topo][basis]:
                            w[(c, l)] = wbs / len(tree[fam][l][b][topo][basis])
    return w


# ----------------------------------------------------------------------------- cost
def cost(S, split=None):
    tot, n, rows = 0.0, 0, []
    for c, (sp, roles) in PANEL.items():
        if split and sp != split:
            continue
        for l, r in roles.items():
            t = S[c]["levels"][l]["t"]
            k = SEEDS[r]
            tot += t * k
            n += k
            rows.append((t * k, c, l, r))
    return tot, n, sorted(rows, reverse=True)


# ----------------------------------------------------------------------------- outputs
def summary(S):
    for split in ("tuning", "validation"):
        w = weights(S, split)
        byfam, bylev, groups = collections.Counter(), collections.Counter(), collections.defaultdict(set)
        for (c, l) in w:
            fam = S[c]["facts"]["family"]
            byfam[fam] += 1
            bylev[l] += 1
            groups[fam].add(c)
        print(f"== {split}: {len(w)} scored cases, {sum(len(g) for g in groups.values())} input groups, sum w = {sum(w.values()):.6f}")
        print("   cases per family:", dict(sorted(byfam.items())), " groups:", {f: len(g) for f, g in sorted(groups.items())})
        print("   cases per level:", dict(sorted(bylev.items())))
        top = sorted(w.items(), key=lambda kv: -kv[1])[:4]
        print("   largest weights:", [(f"{NAME[c]}/L{l}", round(v, 4)) for (c, l), v in top])
        var = sum(v * v * 2 * (S[c]["levels"][l]["sd_lnD2"] or 0.06) ** 2 for (c, l), v in w.items())
        print(f"   planning sd(delta_s) = {math.sqrt(var) * 100:.2f}% -> SE at 100 seeds {math.sqrt(var / 100) * 100:.2f}%")
    t, n, rows = cost(S)
    parts = {sp: cost(S, sp)[0] / 60 for sp in ("tuning", "validation", "guard")}
    print(f"== quality cost per revision and block: {n:,} compiles, {t / 60:.1f} CPU-min "
          f"(tuning {parts['tuning']:.1f}, validation {parts['validation']:.1f}, guards {parts['guard']:.1f})")
    print("   heaviest:", [(f"{NAME[c]}/L{l}/{ROLE_NAME[r]}", f"{x / 60:.1f} min") for x, c, l, r in rows[:6]])


def tables(S):
    hdr = ("| Input | Family | Band | Qubits / `cx` in input | Target | Provenance · source | Levels and roles | "
           "Baseline `D2`, seeds 0–2 | Compile s, L2 / L3 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    def levels_text(roles):
        out = []
        for r in ("S", "G", "D", "Z", "C", "H", "T"):
            ls = [l for l in range(4) if roles.get(l) == r]
            if ls:
                rng = f"{ls[0]}–{ls[-1]}" if len(ls) > 1 else str(ls[0])
                out.append(f"{ROLE_TEXT[r]} {rng}")
        return "; ".join(out)

    for split in ("tuning", "validation", "guard"):
        print(f"\n**{split}**\n\n{hdr}")
        for c, (sp, roles) in PANEL.items():
            if sp != split:
                continue
            f = S[c]["facts"]
            tid, _, _, _ = TARGETS[f["target"]]
            scored = [l for l in range(4) if roles.get(l) == "S"]
            lv = 2 if 2 in scored or not scored else scored[-1]
            d2 = S[c]["levels"][lv]["D2"]
            d2s = f"{min(d2):,}" if min(d2) == max(d2) else f"{min(d2):,}–{max(d2):,}"
            act = active_qubits(c, f)
            q = f"{act} (of {f['n_qubits']})" if act != f["n_qubits"] else str(f["n_qubits"])
            name = f"`{NAME[c]}`" + (" (sabre)" if c.endswith("_sabre") else (" (default)" if c.startswith("queko") else ""))
            tgt = f"`{tid}`" + (" (directed)" if f["target"] in DIRECTED else "")
            print(f"| {name} | {f['family']} | {band(act) if f['family'] != 'canary' else '—'} | {q} / {f['ref_cx']:,} | {tgt} | "
                  f"{f['tier']} · `{SOURCE.get(c, 'transpiler_ft.py')}` | {levels_text(roles)} | {d2s} (L{lv}) | "
                  f"{S[c]['levels'][2]['t']:.2f} / {S[c]['levels'][3]['t']:.2f} |")


def manifest(S):
    w = {}
    for split in ("tuning", "validation"):
        w.update(weights(S, split))
    cases = []
    for c, (sp, roles) in PANEL.items():
        f = S[c]["facts"]
        tid, topo, basis, recipe = TARGETS[f["target"]]
        act = active_qubits(c, f)
        variant = "sabre_methods" if c.endswith("_sabre") else ("default_methods" if c.startswith("queko") else "numeric")
        for l, r in sorted(roles.items()):
            lv = S[c]["levels"][l]
            cases.append(collections.OrderedDict([
                ("case_id", f"confirm/{NAME[c]}{'+sabre' if variant == 'sabre_methods' else ''}/{tid}/L{l}"),
                ("role", ROLE_NAME[r]),
                ("family", f["family"]),
                ("size_band", band(act) if f["family"] != "canary" else None),
                ("logical_qubits", f["n_qubits"]),
                ("active_qubits", act),
                ("topology", topo),
                ("directed", f["target"] in DIRECTED),
                ("native_basis", basis),
                ("optimization_level", l),
                ("input_group", NAME[c]),
                ("split", sp if r == "S" else "guard"),
                ("variant", variant),
                ("provenance", {"grade": f["tier"], "source": "test/benchmarks/" + SOURCE.get(c, "transpiler_ft.py"), "commit": "0131cbbcc"}),
                ("target", {"id": tid, "recipe": recipe,
                            "native_2q_names": [basis] if basis != "clifford_2q" else ["cx", "cz", "cy", "swap", "iswap", "ecr", "dcx"]}),
                ("options", {"approximation_degree": 1.0, "qubits_initially_zero": True,
                             "layout_method": "sabre" if variant == "sabre_methods" else None,
                             "routing_method": "sabre" if variant == "sabre_methods" else None}),
                ("seeds_per_block", SEEDS[r]),
                ("weight", w.get((c, l))),
                ("baseline_probe", {"compile_s_serial_m1max": lv["t"], "D2_seeds_0_2": lv["D2"], "N2_seeds_0_2": lv["N2"],
                                    "seed_sensitive": lv["seed_sensitive"]}),
            ]))
    timing = [{"input_group": NAME[i], "sabre_methods": i.endswith("_sabre"), "mode": "timing_e2e", "optimization_level": l,
               "seed": 0, "baseline_probe_compile_s": S[i]["levels"][l]["t"]}
              for i in TIMING_INPUTS for l in range(4) if not (i == "su2_circ_89" and l == 3)]
    memory = [{"input_group": NAME[i], "sabre_methods": i.endswith("_sabre"), "optimization_level": 2, "processes": 5}
              for i in TIMING_INPUTS + ["hwb12"]]
    scored = [c for c in cases if c["role"] == "scored"]
    doc = collections.OrderedDict([
        ("profile", "confirm-profile"),
        ("status", "draft — proposal; roles must be confirmed from baseline data on TB0 and two calibration "
                   "blocks before freezing (impl plan 3.7, 4.5)"),
        ("baseline", {"qiskit": "2.6.0.dev0", "commit": "0131cbbcc", "fixtures_from": "test/benchmarks/ at that commit"}),
        ("weight_tree_order", ["family", "level", "size_band", "topology", "native_basis", "input_group", "variant"]),
        ("policy", {
            "seeds_per_block": 100,
            "improvement_tuning": "ln(D2_score) + 2*SE < ln(0.99) vs current best and frozen baseline",
            "improvement_validation": "ln(D2_score) + 2*SE < 0 vs current best and frozen baseline, confirmation block only",
            "breadth": ">= 4 of 8 families with ln(family) + 2*SE < 0; leave-one-family-out < 1",
            "guards": "family and level summaries (D2, N2) on both splits, overall N2, per-case caps 1.05, "
                      "per-case SE guards, deterministic/zero-baseline/canary exact",
            "reported_only": ["band summaries", "topology summaries", "basis summaries", "U_instance (family strata)",
                              "family timing summaries"],
            "c1_lite": {"max_active_physical_qubits": 25, "seeds": 10, "operator_up_to": 10,
                        "random_product_states": {"<=16": 8, ">16": 2}},
            "validation_retirement": False, "staleness_warning_after_exposures": 5,
        }),
        ("counts", {"scored_cases": len(scored), "scored_input_groups": len({c["input_group"] for c in scored}),
                    "tuning_cases": sum(1 for c in scored if c["split"] == "tuning"),
                    "validation_cases": sum(1 for c in scored if c["split"] == "validation"),
                    "guard_cases": len(cases) - len(scored), "targets": len(TARGETS) + 1}),
        ("declared_gaps", [
            "line topology: no target in the suite",
            "G4 large, G5 large, G8 large: no in-tree input that compiles in budget",
            "G2 and G7 validation split at levels 1-3: the suite's only inputs are seed-blind (path/ring) or canaries",
            "G7 level 3: su2_circular_n89 is a 10-seed guard (40 s per compile in VF2)",
            "fewer than 3 input groups per family/size cell per split: bootstrap report-only",
            "ecr scored nowhere: guarded through the iterations profile's basis guards only",
        ]),
        ("cases", cases),
        ("timing_panel_additions", timing),
        ("memory_panel", memory),
    ])
    with open(MANIFEST, "w") as fh:
        json.dump(doc, fh, indent=1)
    print(f"wrote {os.path.normpath(MANIFEST)}: {len(cases)} cases, {len(scored)} scored; "
          f"weights sum tuning {sum(c['weight'] for c in scored if c['split'] == 'tuning'):.6f}, "
          f"validation {sum(c['weight'] for c in scored if c['split'] == 'validation'):.6f}")


if __name__ == "__main__":
    S = load()
    summary(S)
    if "--tables" in sys.argv:
        tables(S)
    if "--manifest" in sys.argv:
        manifest(S)
