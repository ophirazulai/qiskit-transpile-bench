"""Inspect the frozen single-panel confirm manifest; never regenerate fixtures."""
from collections import Counter

from qtb.config import load_profile
from qtb.evaluator.statistics import hierarchical_weights


def main():
    manifest, _, hashes = load_profile('confirm-profile')
    scored = [c for c in manifest['cases'] if c['role'] == 'scored']
    expected = hierarchical_weights(scored)
    assert all(abs(c['weight'] - expected[c['case_id']]) < 1e-14 for c in scored)
    print(f"{len(scored)} scored cases; {len({c['input_group'] for c in scored})} input groups")
    print('Families:', dict(Counter(c['family'] for c in scored)))
    print('Levels:', dict(Counter(c['optimization_level'] for c in scored)))
    print('Weight sum:', sum(c['weight'] for c in scored))
    print('Manifest:', hashes['manifest'])
    print('Declared replacements:', manifest.get('replacements', {}))
    print('Coverage gaps:', manifest['coverage_gaps'])


if __name__ == '__main__':
    main()
