"""Per-year DB accessors.

These functions operate on a connection to a single tax_<year>.db opened
via storage.db.connect(). They replace the dict-of-dicts state access
the routes currently do — `state['transactions'][wid][i]` becomes
`get_transaction(conn, wid, i)`, etc.

Callers manage transactions explicitly (`conn.execute("BEGIN") /
"COMMIT"`) around groups of writes. Single-statement writes are
auto-flushed by the autocommit connection in db.py.
"""
import json


# ----------------------------- meta -----------------------------------

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row['value'])


def set_meta(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )


# ----------------------------- wallets --------------------------------

def list_wallets(conn):
    return [
        json.loads(r['data'])
        for r in conn.execute("SELECT data FROM wallets ORDER BY sort_order")
    ]


def list_wallet_ids(conn):
    return [r['id'] for r in conn.execute("SELECT id FROM wallets ORDER BY sort_order")]


def get_wallet(conn, wallet_id):
    row = conn.execute("SELECT data FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
    return json.loads(row['data']) if row else None


def _next_sort_order(conn):
    row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM wallets").fetchone()
    return row[0]


def add_wallet(conn, wallet):
    conn.execute(
        "INSERT INTO wallets(id, sort_order, data) VALUES (?, ?, ?)",
        (wallet['id'], _next_sort_order(conn), json.dumps(wallet)),
    )


def update_wallet(conn, wallet):
    conn.execute(
        "UPDATE wallets SET data = ? WHERE id = ?",
        (json.dumps(wallet), wallet['id']),
    )


def delete_wallet(conn, wallet_id):
    conn.execute("DELETE FROM transactions    WHERE wallet_id = ?", (wallet_id,))
    conn.execute("DELETE FROM reconciliations WHERE wallet_id = ?", (wallet_id,))
    # Classifications use a composite key string; clear by exact prefix.
    prefix = f"{wallet_id}_"
    conn.execute(
        "DELETE FROM classifications WHERE SUBSTR(key, 1, ?) = ?",
        (len(prefix), prefix),
    )
    conn.execute("DELETE FROM wallets WHERE id = ?", (wallet_id,))


# --------------------------- transactions -----------------------------

def list_transactions(conn, wallet_id):
    return [
        json.loads(r['data'])
        for r in conn.execute(
            "SELECT data FROM transactions WHERE wallet_id = ? ORDER BY tx_index",
            (wallet_id,),
        )
    ]


def get_transaction(conn, wallet_id, tx_index):
    row = conn.execute(
        "SELECT data FROM transactions WHERE wallet_id = ? AND tx_index = ?",
        (wallet_id, tx_index),
    ).fetchone()
    return json.loads(row['data']) if row else None


def count_transactions(conn, wallet_id=None):
    if wallet_id is None:
        row = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
    return row[0]


def replace_transactions(conn, wallet_id, txs):
    """Replace all transactions for a wallet with the supplied list."""
    conn.execute("DELETE FROM transactions WHERE wallet_id = ?", (wallet_id,))
    for tx_index, tx in enumerate(txs):
        conn.execute(
            "INSERT INTO transactions(wallet_id, tx_index, date, type, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (wallet_id, tx_index, tx.get('date'), tx.get('type'), json.dumps(tx)),
        )


def upsert_transaction(conn, wallet_id, tx_index, tx):
    conn.execute(
        "INSERT OR REPLACE INTO transactions(wallet_id, tx_index, date, type, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (wallet_id, tx_index, tx.get('date'), tx.get('type'), json.dumps(tx)),
    )


def all_transactions_by_wallet(conn):
    """Return {wallet_id: [tx, ...]} matching the legacy state['transactions'] shape."""
    from collections import defaultdict
    out = defaultdict(list)
    for r in conn.execute(
        "SELECT wallet_id, data FROM transactions ORDER BY wallet_id, tx_index"
    ):
        out[r['wallet_id']].append(json.loads(r['data']))
    return dict(out)


# -------------------------- classifications ---------------------------

def get_classification(conn, key):
    row = conn.execute(
        "SELECT value FROM classifications WHERE key = ?", (key,)
    ).fetchone()
    return row['value'] if row else None


def set_classification(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO classifications(key, value) VALUES (?, ?)",
        (key, value),
    )


def delete_classification(conn, key):
    conn.execute("DELETE FROM classifications WHERE key = ?", (key,))


def list_classifications(conn):
    return {
        r['key']: r['value']
        for r in conn.execute("SELECT key, value FROM classifications")
    }


def classifications_for_wallet(conn, wallet_id):
    """Return {key: value} for every classification on this wallet.

    Classification keys are `{wallet_id}_{tx_index}_{item_index}`. We use
    SUBSTR prefix match (exact char count) so wallet `1` doesn't also pick
    up wallet `10`'s rows.
    """
    prefix = f"{wallet_id}_"
    return {
        r['key']: r['value']
        for r in conn.execute(
            "SELECT key, value FROM classifications WHERE SUBSTR(key, 1, ?) = ?",
            (len(prefix), prefix),
        )
    }


# ----------------------------- positions ------------------------------

def list_positions(conn):
    return [
        json.loads(r['data'])
        for r in conn.execute("SELECT data FROM positions ORDER BY pos_index")
    ]


def count_positions(conn):
    return conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]


def replace_positions(conn, positions):
    conn.execute("DELETE FROM positions")
    for i, p in enumerate(positions):
        conn.execute(
            "INSERT INTO positions(pos_index, data) VALUES (?, ?)",
            (i, json.dumps(p)),
        )


# -------------------------- reconciliations ---------------------------

def get_reconciliation(conn, recon_key):
    row = conn.execute(
        "SELECT data FROM reconciliations WHERE recon_key = ?", (recon_key,)
    ).fetchone()
    return json.loads(row['data']) if row else None


def upsert_reconciliation(conn, recon_key, recon):
    parts = recon_key.split('_')
    wid = parts[0]
    try:
        tx_idx = int(parts[1])
    except (IndexError, ValueError):
        tx_idx = -1
    conn.execute(
        "INSERT OR REPLACE INTO reconciliations(recon_key, wallet_id, tx_index, data) "
        "VALUES (?, ?, ?, ?)",
        (recon_key, wid, tx_idx, json.dumps(recon)),
    )


def delete_reconciliation(conn, recon_key):
    conn.execute("DELETE FROM reconciliations WHERE recon_key = ?", (recon_key,))


def list_reconciliations(conn):
    return {
        r['recon_key']: json.loads(r['data'])
        for r in conn.execute("SELECT recon_key, data FROM reconciliations")
    }


def reconciliations_for_wallet(conn, wallet_id):
    return {
        r['recon_key']: json.loads(r['data'])
        for r in conn.execute(
            "SELECT recon_key, data FROM reconciliations WHERE wallet_id = ?",
            (wallet_id,),
        )
    }


def clear_reconciliations(conn):
    conn.execute("DELETE FROM reconciliations")
