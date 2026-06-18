"""Roll a finished tax year's leftover lots forward as the next year's
opening positions.

Run from project root:

    python scripts/rollover_year.py 2025 2026
    python scripts/rollover_year.py 2025 2026 --force   # overwrite target

What it does:

  1. Load tax_<from_year>.db.
  2. Replay the reconcile pool build (same algorithm the /reconcile page
     uses) — chronologically apply every TRADE/MINT-with-payment sell,
     leaving the lots that *weren't* sold by year end.
  3. Write those leftover lots into tax_<to_year>.db as opening
     positions, preserving original date_acquired and per-unit basis.
  4. Copy the source year's wallet list and known_addresses across, so
     the new year starts with the same setup. Transactions,
     classifications, and reconciliations are NOT copied (those are
     per-year).

Refuses to run if the target year already has opening positions rolled
over from the source year, unless --force is passed.
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

# Pinning the env var so app.py's get_active_year() doesn't try to pick a
# year based on what files exist on disk while we're in the middle of
# creating one. We resolve years explicitly through args.
os.environ.setdefault('TAX_YEAR', '0')

from storage.compat import load_state_dict, save_state_dict  # noqa: E402
from storage import db  # noqa: E402
from app import build_wallet_lot_pools  # noqa: E402


EPS = 1e-12


def rollover(from_year, to_year, force=False):
    src = load_state_dict(from_year)
    if not src:
        sys.exit(f"No data for {from_year} — run scripts/migrate_to_sqlite.py first.")

    method = src.get('cost_basis_method', 'LIFO')
    print(f"Rolling {from_year} → {to_year} using {method}.")

    # Replay the reconcile pool build. With default args, this consumes
    # every TRADE / MINT-payment sell, leaving the unsold lots per wallet.
    pools = build_wallet_lot_pools(src, method)

    # Turn the leftover lots into opening positions for the next year.
    leftover_positions = []
    skipped = 0
    for wallet_addr, lots in pools.items():
        for lot in lots:
            volume = lot.get('volume', 0)
            if volume <= EPS:
                skipped += 1
                continue
            price = lot.get('price', 0)
            leftover_positions.append({
                'symbol':      lot.get('symbol', ''),
                'date':        lot.get('date', ''),
                'volume':      str(volume),
                'price':       str(price),
                'total':       str(volume * price),
                'account':     wallet_addr,
                'currency':    'USD',
                'fee':         '0',
                'fee_currency':'USD',
                'memo':        f"rollover_from_{from_year} ({lot.get('source', '')})",
            })

    print(f"  source lots in pools: {sum(len(v) for v in pools.values())}")
    print(f"  empty/dust skipped:   {skipped}")
    print(f"  rollover positions:   {len(leftover_positions)}")

    # Bootstrap and load the target year's DB (will be empty if first run).
    db.bootstrap_year_db(to_year)
    dst = load_state_dict(to_year) or {}

    # Refuse to clobber an existing rollover unless --force.
    existing_rollover = [
        p for p in dst.get('positions', [])
        if str(p.get('memo', '')).startswith(f"rollover_from_{from_year}")
    ]
    if existing_rollover and not force:
        sys.exit(
            f"Target {to_year} already has {len(existing_rollover)} positions rolled over "
            f"from {from_year}. Re-run with --force to replace them.")

    # Drop any prior rollover-from-this-year and keep any user-added positions.
    kept = [
        p for p in dst.get('positions', [])
        if not str(p.get('memo', '')).startswith(f"rollover_from_{from_year}")
    ]
    dst['positions'] = kept + leftover_positions
    dst['wallets'] = src.get('wallets', [])
    dst['known_addresses'] = src.get('known_addresses', [])
    dst['cost_basis_method'] = method
    dst['tax_year'] = to_year
    dst.setdefault('transactions', {})
    dst.setdefault('classifications', {})
    dst.setdefault('reconciliations', {})
    dst.setdefault('price_cache', {})
    dst.setdefault('filename', f"rollover_from_{from_year}")

    save_state_dict(dst, to_year)
    print(f"\nWrote {len(dst['positions'])} positions, {len(dst['wallets'])} wallets, "
          f"{len(dst['known_addresses'])} known-address groups into {db.year_db_path(to_year)}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('from_year', type=int, help='source tax year (e.g., 2025)')
    ap.add_argument('to_year',   type=int, help='target tax year (e.g., 2026)')
    ap.add_argument('--force', action='store_true',
                    help='replace any existing rollover in the target year')
    args = ap.parse_args()
    if args.to_year <= args.from_year:
        sys.exit(f"to_year ({args.to_year}) must be after from_year ({args.from_year}).")
    rollover(args.from_year, args.to_year, force=args.force)


if __name__ == '__main__':
    main()
