"""Verify decisions reproduce from their archived observations, without recompiling."""
import sys
from pathlib import Path

from qtb.canonical import canonical_bytes, read_json
from qtb.coordinator import evaluate_run

root = Path(sys.argv[1])
for path in sorted(root.glob('runs/*/decision.json')):
    before = canonical_bytes(read_json(path))
    after = canonical_bytes(evaluate_run(path.parent))
    if before != after:
        raise SystemExit(f'Decision replay differs: {path}')
    print(f'Reproduced {path}')
