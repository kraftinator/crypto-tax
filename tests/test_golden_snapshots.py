"""Golden-snapshot tests — the migration safety net.

Run from project root: `python -m unittest tests.test_golden_snapshots`

Re-runs the same end-to-end computations as generate_golden.py against the
current state, and asserts the output still matches the snapshots on disk.

If a test fails after a migration phase, the diff between the live output
and the golden tells you exactly what changed. To regenerate the goldens
(after an intentional behavior change), re-run `python tests/generate_golden.py`.
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

# Goldens were captured against 2025 data; pin the active year so this test
# stays correct even after a 2026 year DB exists.
os.environ['TAX_YEAR'] = '2025'

from app import (  # noqa: E402
    load_state,
    build_form8949_rows,
    build_income_rows,
    build_2025_lots,
)
from tests.generate_golden import compute_totals  # noqa: E402

GOLDEN_DIR = os.path.join(PROJECT_ROOT, 'tests', 'golden')
PER_WALLET_DIR = os.path.join(GOLDEN_DIR, 'per_wallet')


def _load_golden(path):
    """Round-trip through JSON to canonicalize types (e.g., tuple→list)."""
    with open(path) as f:
        return json.load(f)


def _canon(obj):
    """Same serialization shape as generate_golden uses, so comparisons match."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


class GoldenSnapshotTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(GOLDEN_DIR):
            raise unittest.SkipTest(
                f"No goldens at {GOLDEN_DIR}/ — run `python tests/generate_golden.py` first."
            )
        cls.state = load_state()
        if not cls.state:
            raise unittest.SkipTest("No state.json found.")

    # ---- 1) End-to-end ----------------------------------------------------

    def test_form8949_rows_match_golden(self):
        rows = build_form8949_rows(self.state)
        golden = _load_golden(os.path.join(GOLDEN_DIR, 'form8949_rows.json'))
        self.assertEqual(_canon(rows), golden, "Form 8949 rows diverged from golden")

    def test_form8949_totals_match_golden(self):
        rows = build_form8949_rows(self.state)
        totals = compute_totals(rows)
        golden = _load_golden(os.path.join(GOLDEN_DIR, 'form8949_totals.json'))
        self.assertEqual(_canon(totals), golden, "Form 8949 totals diverged from golden")

    def test_income_rows_match_golden(self):
        rows = build_income_rows(self.state)
        golden = _load_golden(os.path.join(GOLDEN_DIR, 'income_rows.json'))
        self.assertEqual(_canon(rows), golden, "Income rows diverged from golden")

    # ---- 2) Per-wallet ----------------------------------------------------

    def test_per_wallet_lots_match_golden(self):
        wallets = self.state.get('wallets', [])
        mismatches = []
        for w in wallets:
            wid = w['id']
            lots = build_2025_lots(self.state, wid)
            golden_path = os.path.join(PER_WALLET_DIR, f'wallet_{wid}_lots.json')
            if not os.path.exists(golden_path):
                mismatches.append(f"missing golden for wallet {wid}")
                continue
            golden = _load_golden(golden_path)
            if _canon(lots) != golden:
                mismatches.append(f"wallet {wid} lots diverged ({len(lots)} live vs {len(golden)} golden)")
        self.assertFalse(mismatches, "Per-wallet lot snapshots diverged:\n  " + "\n  ".join(mismatches))

    def test_per_wallet_reconcile_match_golden(self):
        from collections import defaultdict
        recons = self.state.get('reconciliations', {})
        by_wallet = defaultdict(dict)
        for key, recon in recons.items():
            wid = key.split('_', 1)[0]
            by_wallet[wid][key] = recon

        mismatches = []
        for w in self.state.get('wallets', []):
            wid = w['id']
            golden_path = os.path.join(PER_WALLET_DIR, f'wallet_{wid}_reconcile.json')
            if not os.path.exists(golden_path):
                mismatches.append(f"missing golden for wallet {wid}")
                continue
            golden = _load_golden(golden_path)
            live = _canon(by_wallet.get(wid, {}))
            if live != golden:
                mismatches.append(f"wallet {wid} reconcile diverged ({len(live)} live vs {len(golden)} golden)")
        self.assertFalse(mismatches, "Per-wallet reconcile snapshots diverged:\n  " + "\n  ".join(mismatches))


if __name__ == '__main__':
    unittest.main()
