"""Compatibility shim — assemble the legacy state.json dict shape from SQLite.

Phase 1 of the SQLite migration leaves all route code unchanged. Routes still
do `state = load_state()` and treat `state` as a dict. Internally,
`load_state()` now calls `load_state_dict()` here instead of parsing JSON.

When all routes have been moved to repo-level accessors (Phase 3), this
module goes away.
"""
import json
import os
from collections import defaultdict

from storage import db


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
