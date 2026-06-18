"""One-shot migration: data/state.json -> tax_<year>.db + shared.db.

Run from project root: `python scripts/migrate_to_sqlite.py`

Reads `data/state.json`, takes the year from `state['tax_year']`, and writes
the contents into `data/tax_<year>.db` plus `data/shared.db` (price cache).

Refuses to run if the target year DB is already populated unless --force is
given (the --force path wipes the year DB first).
"""
import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

from storage import db  # noqa: E402
from storage.compat import save_state_dict  # noqa: E402


DEFAULT_STATE_PATH = os.path.join(PROJECT_ROOT, 'data', 'state.json')


def _is_year_db_populated(conn):
    row = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()
    return row[0] > 0


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
        if _is_year_db_populated(ydb) and not force:
            sys.exit(f"{year_path} already has data — re-run with --force to wipe.")

    save_state_dict(state, year)

    # Print counts so the operator can sanity-check.
    with db.connect(year_path) as ydb:
        for tbl in ('meta', 'wallets', 'transactions', 'classifications',
                    'positions', 'reconciliations'):
            n = ydb.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl:18s} {n}")
    with db.connect(shared_path) as sdb:
        n = sdb.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0]
        print(f"  {'price_cache':18s} {n}")

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
