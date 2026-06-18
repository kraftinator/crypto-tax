"""Generate golden snapshots from the current state.json.

Run from project root: `python tests/generate_golden.py`

Writes to tests/golden/ (gitignored). The companion test file
test_golden_snapshots.py re-runs the same computations and asserts the
results still match these files — the migration safety net.
"""
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

# Goldens are derived from the 2025 dataset; pin the active year so this
# generator stays correct even when other year DBs exist.
os.environ['TAX_YEAR'] = '2025'

# Importing app.py registers Flask routes as a side effect. That's fine; we
# only call the pure functions exported from it.
from app import (  # noqa: E402
    load_state,
    build_form8949_rows,
    build_income_rows,
    build_2025_lots,
    determine_term,
)

GOLDEN_DIR = os.path.join(PROJECT_ROOT, 'tests', 'golden')
PER_WALLET_DIR = os.path.join(GOLDEN_DIR, 'per_wallet')


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f, indent=2, sort_keys=True, default=str)


def compute_totals(rows):
    """Aggregate Form 8949 rows into short/long totals."""
    totals = {
        'short': {'count': 0, 'proceeds': 0.0, 'basis': 0.0, 'gain_loss': 0.0},
        'long':  {'count': 0, 'proceeds': 0.0, 'basis': 0.0, 'gain_loss': 0.0},
    }
    for r in rows:
        bucket = 'long' if r.get('term') == 'long' else 'short'
        totals[bucket]['count']     += 1
        totals[bucket]['proceeds']  += r.get('proceeds', 0)
        totals[bucket]['basis']     += r.get('cost_basis', 0)
        totals[bucket]['gain_loss'] += r.get('gain_loss', 0)
    # Round at the end to mirror Form 8949 reporting precision.
    for b in totals.values():
        b['proceeds']  = round(b['proceeds'],  2)
        b['basis']     = round(b['basis'],     2)
        b['gain_loss'] = round(b['gain_loss'], 2)
    return totals


def main():
    state = load_state()
    if not state:
        sys.exit(f"No state at {os.path.join('data', 'state.json')} — nothing to snapshot.")

    wallets = state.get('wallets', [])
    print(f"Loaded state: {len(wallets)} wallets, "
          f"{sum(len(t) for t in state.get('transactions', {}).values())} transactions, "
          f"{len(state.get('reconciliations', {}))} reconciliations, "
          f"{len(state.get('classifications', {}))} classifications")

    # 1) End-to-end: Form 8949 rows + totals
    rows = build_form8949_rows(state)
    _dump(os.path.join(GOLDEN_DIR, 'form8949_rows.json'), rows)
    print(f"  form8949_rows.json: {len(rows)} rows")

    totals = compute_totals(rows)
    _dump(os.path.join(GOLDEN_DIR, 'form8949_totals.json'), totals)
    print(f"  form8949_totals.json: "
          f"short ${totals['short']['gain_loss']:,.2f} / "
          f"long ${totals['long']['gain_loss']:,.2f}")

    # 2) End-to-end: income rows
    income = build_income_rows(state)
    _dump(os.path.join(GOLDEN_DIR, 'income_rows.json'), income)
    print(f"  income_rows.json: {len(income)} rows")

    # 3) Per-wallet lots (build_2025_lots — yields lots from RECEIVE/TRADE/MINT classifications)
    # 4) Per-wallet reconciliations (filtered slice of state['reconciliations'])
    recons = state.get('reconciliations', {})
    by_wallet = defaultdict(dict)
    for key, recon in recons.items():
        wallet_id = key.split('_', 1)[0]
        by_wallet[wallet_id][key] = recon

    for w in wallets:
        wid = w['id']
        lots = build_2025_lots(state, wid)
        _dump(os.path.join(PER_WALLET_DIR, f'wallet_{wid}_lots.json'), lots)

        wallet_recons = by_wallet.get(wid, {})
        _dump(os.path.join(PER_WALLET_DIR, f'wallet_{wid}_reconcile.json'), wallet_recons)

        print(f"  wallet_{wid}: {len(lots)} lots, {len(wallet_recons)} reconciliations")

    print(f"\nWrote goldens to {GOLDEN_DIR}/ (gitignored).")


if __name__ == '__main__':
    main()
