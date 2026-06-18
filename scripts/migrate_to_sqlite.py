"""One-shot migration: data/state.json -> tax_<year>.db + shared.db.

Run from project root: `python scripts/migrate_to_sqlite.py`

By default reads `data/state.json` and writes whatever year is in
`state['tax_year']` into `data/tax_<year>.db`, plus `data/shared.db` for
the cross-year price cache.

Refuses to run if the target year DB already has data (use --force to
wipe it first).
"""
import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

from storage import db  # noqa: E402


DEFAULT_STATE_PATH = os.path.join(PROJECT_ROOT, 'data', 'state.json')


def _is_year_db_populated(conn):
    row = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()
    return row[0] > 0


def _wipe_year(conn):
    conn.execute("BEGIN")
    for tbl in ('reconciliations', 'positions', 'classifications',
                'transactions', 'wallets', 'meta'):
        conn.execute(f"DELETE FROM {tbl}")
    conn.execute("COMMIT")


def migrate(state_path=DEFAULT_STATE_PATH, force=False):
    with open(state_path) as f:
        state = json.load(f)

    year = state.get('tax_year')
    if not year:
        sys.exit(f"state at {state_path} has no tax_year — bailing.")

    year_path = db.bootstrap_year_db(year)
    shared_path = db.bootstrap_shared_db()
    print(f"Year DB:   {year_path}")
    print(f"Shared DB: {shared_path}")

    with db.connect(year_path) as ydb:
        if _is_year_db_populated(ydb):
            if not force:
                sys.exit(f"{year_path} already has data — re-run with --force to wipe.")
            print("--force given; wiping year DB.")
            _wipe_year(ydb)

        ydb.execute("BEGIN")
        try:
            # ---- meta -------------------------------------------------
            for k in ('tax_year', 'cost_basis_method', 'filename'):
                if k in state:
                    ydb.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (k, json.dumps(state[k])),
                    )
            # known_addresses is a list — fold into meta as a JSON blob so
            # the compat shim can restore the exact shape with no joins.
            if 'known_addresses' in state:
                ydb.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ('known_addresses', json.dumps(state['known_addresses'])),
                )

            # ---- wallets ----------------------------------------------
            for i, w in enumerate(state.get('wallets', [])):
                ydb.execute(
                    "INSERT INTO wallets(id, sort_order, data) VALUES (?, ?, ?)",
                    (w['id'], i, json.dumps(w)),
                )

            # ---- transactions ----------------------------------------
            tx_count = 0
            for wallet_id, txs in state.get('transactions', {}).items():
                for tx_index, tx in enumerate(txs):
                    ydb.execute(
                        "INSERT INTO transactions(wallet_id, tx_index, date, type, data) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            wallet_id,
                            tx_index,
                            tx.get('date'),
                            tx.get('type'),
                            json.dumps(tx),
                        ),
                    )
                    tx_count += 1

            # ---- classifications --------------------------------------
            cls_count = 0
            for key, value in state.get('classifications', {}).items():
                ydb.execute(
                    "INSERT INTO classifications(key, value) VALUES (?, ?)",
                    (key, value),
                )
                cls_count += 1

            # ---- positions --------------------------------------------
            for i, p in enumerate(state.get('positions', [])):
                ydb.execute(
                    "INSERT INTO positions(pos_index, data) VALUES (?, ?)",
                    (i, json.dumps(p)),
                )

            # ---- reconciliations --------------------------------------
            recon_count = 0
            for key, recon in state.get('reconciliations', {}).items():
                parts = key.split('_')
                wid = parts[0]
                try:
                    tx_idx = int(parts[1])
                except (IndexError, ValueError):
                    tx_idx = -1
                ydb.execute(
                    "INSERT INTO reconciliations(recon_key, wallet_id, tx_index, data) "
                    "VALUES (?, ?, ?, ?)",
                    (key, wid, tx_idx, json.dumps(recon)),
                )
                recon_count += 1

            ydb.execute("COMMIT")
        except Exception:
            ydb.execute("ROLLBACK")
            raise

        print(f"  meta keys:        {ydb.execute('SELECT COUNT(*) FROM meta').fetchone()[0]}")
        print(f"  wallets:          {len(state.get('wallets', []))}")
        print(f"  transactions:     {tx_count}")
        print(f"  classifications:  {cls_count}")
        print(f"  positions:        {len(state.get('positions', []))}")
        print(f"  reconciliations:  {recon_count}")

    # ---- shared.db: price_cache --------------------------------------
    with db.connect(shared_path) as sdb:
        sdb.execute("BEGIN")
        try:
            sdb.execute("DELETE FROM price_cache")
            pc_count = 0
            for key, price in state.get('price_cache', {}).items():
                sdb.execute(
                    "INSERT INTO price_cache(cache_key, price) VALUES (?, ?)",
                    (key, price),
                )
                pc_count += 1
            sdb.execute("COMMIT")
        except Exception:
            sdb.execute("ROLLBACK")
            raise
        print(f"  price_cache:      {pc_count}")

    print("\nMigration complete.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--state', default=DEFAULT_STATE_PATH,
                    help='path to state.json (default: data/state.json)')
    ap.add_argument('--force', action='store_true',
                    help='wipe target year DB before migrating')
    args = ap.parse_args()
    migrate(args.state, force=args.force)


if __name__ == '__main__':
    main()
