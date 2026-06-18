"""Compatibility shim — bridge the legacy state.json dict shape and SQLite.

Phase 1/2 of the migration leaves all route code unchanged. Routes still
do `state = load_state()` / `save_state(state)` and treat `state` as a
single dict. Internally, those calls now go through `load_state_dict()`
and `save_state_dict()` here.

When all routes have been moved to per-table accessors (Phase 3), this
module goes away.
"""
import json
import os
from collections import defaultdict

from storage import db


_YEAR_TABLES_FOR_WIPE = (
    'reconciliations', 'positions', 'classifications',
    'transactions', 'wallets', 'meta',
)


def load_state_dict(year):
    """Return a dict in the exact shape state.json used to produce."""
    year_path = db.year_db_path(year)
    shared_path = db.shared_db_path()

    if not os.path.exists(year_path):
        return None

    state = {}

    with db.connect(year_path) as ydb:
        # ---- meta -----------------------------------------------------
        for row in ydb.execute("SELECT key, value FROM meta"):
            state[row['key']] = json.loads(row['value'])

        # ---- wallets --------------------------------------------------
        state['wallets'] = [
            json.loads(r['data'])
            for r in ydb.execute("SELECT data FROM wallets ORDER BY sort_order")
        ]

        # ---- transactions --------------------------------------------
        tx_by_wallet = defaultdict(list)
        cur = ydb.execute(
            "SELECT wallet_id, tx_index, data FROM transactions "
            "ORDER BY wallet_id, tx_index"
        )
        for r in cur:
            tx_by_wallet[r['wallet_id']].append(json.loads(r['data']))
        # Preserve the wallet ordering used by save_state — iterate in wallet
        # sort order so the dict's insertion order matches the legacy file.
        state['transactions'] = {
            w['id']: tx_by_wallet.get(w['id'], [])
            for w in state['wallets']
        }
        # Include any orphan wallet_ids that exist in transactions but not in
        # wallets (shouldn't happen, but preserve them if they do).
        for wid in tx_by_wallet:
            if wid not in state['transactions']:
                state['transactions'][wid] = tx_by_wallet[wid]

        # ---- classifications ------------------------------------------
        state['classifications'] = {
            r['key']: r['value']
            for r in ydb.execute("SELECT key, value FROM classifications")
        }

        # ---- positions ------------------------------------------------
        state['positions'] = [
            json.loads(r['data'])
            for r in ydb.execute("SELECT data FROM positions ORDER BY pos_index")
        ]

        # ---- reconciliations ------------------------------------------
        state['reconciliations'] = {
            r['recon_key']: json.loads(r['data'])
            for r in ydb.execute("SELECT recon_key, data FROM reconciliations")
        }

    # ---- shared.db: price_cache ---------------------------------------
    state['price_cache'] = {}
    if os.path.exists(shared_path):
        with db.connect(shared_path) as sdb:
            for r in sdb.execute("SELECT cache_key, price FROM price_cache"):
                state['price_cache'][r['cache_key']] = r['price']

    return state


def save_state_dict(state, year):
    """Persist a whole state dict to SQLite. Replaces every row.

    Phase 2 dump-and-replace: matches the existing 'load → mutate → save'
    code idiom one-for-one. Per-mutation writes come in Phase 3, when
    routes stop passing the whole dict around.
    """
    year_path = db.bootstrap_year_db(year)
    shared_path = db.bootstrap_shared_db()

    with db.connect(year_path) as ydb:
        ydb.execute("BEGIN")
        try:
            for tbl in _YEAR_TABLES_FOR_WIPE:
                ydb.execute(f"DELETE FROM {tbl}")

            # ---- meta -------------------------------------------------
            for k in ('tax_year', 'cost_basis_method', 'filename'):
                if k in state:
                    ydb.execute(
                        "INSERT INTO meta(key, value) VALUES (?, ?)",
                        (k, json.dumps(state[k])),
                    )
            if 'known_addresses' in state:
                ydb.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    ('known_addresses', json.dumps(state['known_addresses'])),
                )

            # ---- wallets ----------------------------------------------
            for i, w in enumerate(state.get('wallets', [])):
                ydb.execute(
                    "INSERT INTO wallets(id, sort_order, data) VALUES (?, ?, ?)",
                    (w['id'], i, json.dumps(w)),
                )

            # ---- transactions ----------------------------------------
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

            # ---- classifications --------------------------------------
            for key, value in state.get('classifications', {}).items():
                ydb.execute(
                    "INSERT INTO classifications(key, value) VALUES (?, ?)",
                    (key, value),
                )

            # ---- positions --------------------------------------------
            for i, p in enumerate(state.get('positions', [])):
                ydb.execute(
                    "INSERT INTO positions(pos_index, data) VALUES (?, ?)",
                    (i, json.dumps(p)),
                )

            # ---- reconciliations --------------------------------------
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

            ydb.execute("COMMIT")
        except Exception:
            ydb.execute("ROLLBACK")
            raise

    # ---- shared.db: price_cache ---------------------------------------
    with db.connect(shared_path) as sdb:
        sdb.execute("BEGIN")
        try:
            sdb.execute("DELETE FROM price_cache")
            for key, price in state.get('price_cache', {}).items():
                sdb.execute(
                    "INSERT INTO price_cache(cache_key, price) VALUES (?, ?)",
                    (key, price),
                )
            sdb.execute("COMMIT")
        except Exception:
            sdb.execute("ROLLBACK")
            raise
