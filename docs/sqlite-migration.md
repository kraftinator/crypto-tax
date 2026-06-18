# SQLite migration + multi-year support

Design doc for migrating the crypto-tax app off `data/state.json` and onto
per-year SQLite databases, so tax year 2026 can be added without entangling
the (filed) 2025 data.

## Goals

1. End per-request 44MB JSON parse on every page load.
2. Make 2025 a frozen, archived dataset; make 2026 the active dataset.
3. Keep the workflow identical from the user's point of view (upload CSV →
   classify → reconcile → generate Form 8949).
4. Make year rollover explicit: end-of-year unmatched lots from 2025 become
   the opening positions for 2026.
5. Remove the cosmetic hardcoding of "2025" in function and variable names.

## Non-goals

- No new dependencies. `sqlite3` is stdlib.
- No multi-user / auth / network DB. Single-user local app stays that way.
- No ORM. Hand-written SQL through small repo helpers.
- No support for editing a prior year's classifications via the UI in this
  phase. Prior years are read-only archives.

## Storage layout

```
data/
├── tax_2025.db     # archived after filing — read-only from the app
├── tax_2026.db     # active year
└── shared.db       # cross-year: price_cache, known_addresses
```

The active year is selected by:

1. `TAX_YEAR` env var if set, else
2. The latest `data/tax_*.db` file present, else
3. Falls back to creating `tax_<current_year>.db`.

Backups continue under `data/backups/` as `tax_2026_YYYYMMDD_HHMMSS.db` (file
copy, since SQLite handles atomicity itself).

## Schema

### Per-year DB (`tax_<year>.db`)

```sql
PRAGMA user_version = 1;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- rows: tax_year, cost_basis_method, opening_positions_filename, created_at

CREATE TABLE wallets (
  id         TEXT PRIMARY KEY,
  address    TEXT NOT NULL,
  blockchain TEXT,
  name       TEXT,
  sort_order INTEGER DEFAULT 0
);

CREATE TABLE transactions (
  wallet_id      TEXT NOT NULL,
  tx_index       INTEGER NOT NULL,
  date           TEXT NOT NULL,             -- ISO 8601
  type           TEXT NOT NULL,             -- TRADE / RECEIVE / SEND / MINT / BURN / ...
  sender         TEXT,
  recipient      TEXT,
  blockchain     TEXT,
  tx_hash        TEXT,
  manual         INTEGER NOT NULL DEFAULT 0,
  parsed_details TEXT NOT NULL,             -- JSON: {sent: [...], received: [...]}
  raw            TEXT,                      -- JSON: original CSV row (for audit)
  PRIMARY KEY (wallet_id, tx_index),
  FOREIGN KEY (wallet_id) REFERENCES wallets(id)
);
CREATE INDEX ix_tx_wallet_date ON transactions(wallet_id, date);
CREATE INDEX ix_tx_type        ON transactions(type);
CREATE INDEX ix_tx_date        ON transactions(date);

CREATE TABLE classifications (
  wallet_id      TEXT NOT NULL,
  tx_index       INTEGER NOT NULL,
  item_index     INTEGER NOT NULL,
  classification TEXT NOT NULL,             -- Income / Airdrop / Transfer / Payment / LP Deposit / ...
  PRIMARY KEY (wallet_id, tx_index, item_index),
  FOREIGN KEY (wallet_id, tx_index) REFERENCES transactions(wallet_id, tx_index)
);

CREATE TABLE positions (
  -- opening lots imported from BitcoinTax CSV at year start
  id           INTEGER PRIMARY KEY,
  symbol       TEXT NOT NULL,
  date_acquired TEXT NOT NULL,
  volume       REAL NOT NULL,
  basis_usd    REAL NOT NULL,
  account      TEXT,                        -- exchange/wallet label from BitcoinTax
  source       TEXT                         -- 'opening' or 'rollover_from_<prev_year>'
);
CREATE INDEX ix_pos_symbol ON positions(symbol);

CREATE TABLE reconciliations (
  recon_key  TEXT PRIMARY KEY,              -- '<wallet_id>_<tx_index>'
  wallet_id  TEXT NOT NULL,
  tx_index   INTEGER NOT NULL,
  proceeds   REAL NOT NULL,
  method     TEXT NOT NULL,                 -- LIFO / FIFO at time of reconcile
  lots_used  TEXT NOT NULL,                 -- JSON: [{lot_id, source, volume_used, cost_basis, date_acquired}, ...]
  created_at TEXT NOT NULL,
  FOREIGN KEY (wallet_id, tx_index) REFERENCES transactions(wallet_id, tx_index)
);
CREATE INDEX ix_recon_wallet ON reconciliations(wallet_id);
```

### Cross-year DB (`shared.db`)

```sql
PRAGMA user_version = 1;

CREATE TABLE price_cache (
  coingecko_id TEXT NOT NULL,
  date         TEXT NOT NULL,               -- YYYY-MM-DD
  price_usd    REAL,                        -- null = lookup failed
  source       TEXT,                        -- 'coingecko' / 'defillama' / 'trade_inference'
  contract     TEXT,
  blockchain   TEXT,
  looked_up_at TEXT NOT NULL,
  PRIMARY KEY (coingecko_id, date)
);
CREATE INDEX ix_price_date ON price_cache(date);

CREATE TABLE known_addresses (
  address    TEXT PRIMARY KEY,              -- normalized lowercase
  label      TEXT NOT NULL,
  blockchain TEXT
);

CREATE TABLE failed_lookups (
  -- mirrors current 'failed_tokens' set; lets us skip retries cheaply
  coingecko_id TEXT PRIMARY KEY,
  reason       TEXT,
  last_tried   TEXT NOT NULL
);
```

### What `state.json` keys map where

| `state.json` key       | Destination                                  |
| ---------------------- | -------------------------------------------- |
| `tax_year`             | `tax_YYYY.db` filename + `meta` row          |
| `cost_basis_method`    | `tax_YYYY.db:meta`                           |
| `filename`             | `tax_YYYY.db:meta` (opening_positions_filename) |
| `wallets`              | `tax_YYYY.db:wallets`                        |
| `transactions[wid]`    | `tax_YYYY.db:transactions`                   |
| `classifications`      | `tax_YYYY.db:classifications`                |
| `positions`            | `tax_YYYY.db:positions`                      |
| `reconciliations`      | `tax_YYYY.db:reconciliations`                |
| `price_cache`          | `shared.db:price_cache`                      |
| `known_addresses`      | `shared.db:known_addresses`                  |
| (in-memory `failed_tokens` set during `fill_missing_usd_values`) | `shared.db:failed_lookups` |

## Module layout

```
crypto-tax/
├── app.py                          # routes (unchanged shape; thinned of state plumbing)
├── inspect_form.py                 # unchanged
├── storage/
│   ├── __init__.py
│   ├── db.py                       # connection helpers, schema bootstrap, migrations
│   ├── repo_year.py                # per-year table accessors (wallets, txs, ...)
│   ├── repo_shared.py              # shared.db accessors (price_cache, known_addresses)
│   └── migrations/
│       ├── year_0001_initial.sql
│       └── shared_0001_initial.sql
├── scripts/
│   ├── migrate_to_sqlite.py        # one-off: state.json → tax_2025.db + shared.db
│   └── rollover_year.py            # one-off: end-of-year unmatched lots → next year's opening positions
├── templates/                      # unchanged
├── data/                           # gitignored
└── docs/
    └── sqlite-migration.md         # this doc
```

### `storage/db.py` — sketch

```python
import sqlite3, threading, os
from contextlib import contextmanager

_locks = {}
_lock_guard = threading.Lock()

def _lock_for(path):
    with _lock_guard:
        if path not in _locks:
            _locks[path] = threading.RLock()
        return _locks[path]

@contextmanager
def connect(path):
    """Yield a sqlite3.Connection with sensible pragmas, serialized per-file."""
    with _lock_for(path):
        conn = sqlite3.connect(path, isolation_level=None)  # autocommit; we use explicit BEGIN
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

def year_db_path(year):
    return os.path.join(os.path.dirname(__file__), '..', 'data', f'tax_{year}.db')

def shared_db_path():
    return os.path.join(os.path.dirname(__file__), '..', 'data', 'shared.db')

def bootstrap(path, migrations_dir, prefix):
    """Apply numbered .sql migrations if user_version is behind."""
    ...
```

### `storage/repo_year.py` — sketch

Small focused functions, one per access pattern. Returns plain dicts / lists
so route code stays close to its current shape:

```python
def list_wallets(conn) -> list[dict]: ...
def get_wallet(conn, wallet_id) -> dict | None: ...
def add_wallet(conn, wallet) -> None: ...

def list_transactions(conn, wallet_id) -> list[dict]: ...
def get_transaction(conn, wallet_id, tx_index) -> dict | None: ...
def replace_transactions(conn, wallet_id, txs: list[dict]) -> None:
    # used by CSV re-upload (preserves manual rows separately)
    ...
def upsert_transaction(conn, wallet_id, tx_index, tx) -> None: ...

def get_classifications(conn, wallet_id, tx_index) -> dict[int, str]: ...
def set_classification(conn, wallet_id, tx_index, item_index, classification) -> None: ...
def all_classifications(conn) -> dict[str, str]:  # {item_key: classification} for compatibility
    ...

def list_positions(conn) -> list[dict]: ...
def replace_positions(conn, positions: list[dict]) -> None: ...

def get_reconciliation(conn, recon_key) -> dict | None: ...
def save_reconciliation(conn, recon) -> None: ...
def all_reconciliations(conn) -> dict[str, dict]: ...

def get_meta(conn, key, default=None) -> str | None: ...
def set_meta(conn, key, value) -> None: ...
```

### `storage/repo_shared.py` — sketch

```python
def get_price(conn, coingecko_id, date) -> float | None: ...
def set_price(conn, coingecko_id, date, price, source, contract=None, blockchain=None) -> None: ...
def get_prices_bulk(conn, items: list[tuple[str, str]]) -> dict[tuple[str,str], float]: ...

def is_failed(conn, coingecko_id) -> bool: ...
def mark_failed(conn, coingecko_id, reason) -> None: ...

def list_known_addresses(conn) -> dict[str, str]: ...
def add_known_address(conn, address, label, blockchain=None) -> None: ...
```

## App integration

The Flask app currently does this in nearly every route:

```python
state = load_state()
... state['transactions'][wid].append(...)
save_state(state)
```

The replacement pattern is:

```python
with db.connect(db.year_db_path(active_year)) as conn:
    conn.execute("BEGIN")
    try:
        repo_year.add_transaction(conn, wid, tx)
        conn.execute("COMMIT")
    except:
        conn.execute("ROLLBACK")
        raise
```

To keep the diff per-route small, we introduce a request-scoped helper:

```python
@app.before_request
def _open_db():
    g.year_conn = db.connect(db.year_db_path(get_active_year())).__enter__()
    g.shared_conn = db.connect(db.shared_db_path()).__enter__()

@app.teardown_request
def _close_db(exc):
    # close both; rollback open transactions on exception
    ...
```

So routes just use `g.year_conn` / `g.shared_conn`. No mega-state dict.

### "2025" rename

Once the rename is mechanical, we drop the year from identifiers:

| Before                  | After                  |
| ----------------------- | ---------------------- |
| `build_2025_lots(...)`  | `build_year_lots(...)` |
| `lots_2025`             | `year_lots`            |
| `token_2025_lots`       | `token_year_lots`      |
| `matched_2025_ids`      | `matched_year_ids`     |
| `'2025_trade'` (source) | `'trade'`              |
| `'2025_airdrop'`        | `'airdrop'`            |
| `'2025_income'`         | `'income'`             |
| `'2025_unstaking'`      | `'unstaking'`          |
| `'2025_mint'`           | `'mint'`               |

(Year is now implicit in which DB you're connected to.)

## Year rollover

`scripts/rollover_year.py 2025 2026`:

1. Open `tax_2025.db` read-only.
2. Re-run the reconcile pool build (same algorithm as `/reconcile`) to produce
   the final set of unmatched lots as of `2025-12-31 23:59:59 UTC`.
3. For each unmatched lot: emit a row into `tax_2026.db:positions` with
   `source = 'rollover_from_2025'`, preserving original `date_acquired` and
   `basis_usd` (basis carries forward — that's how cost basis works).
4. Copy `wallets` rows into `tax_2026.db`. (User keeps the same wallets.)
5. Set `tax_2026.db:meta` with `cost_basis_method` matching 2025.
6. Print a summary: N lots rolled, total basis, distinct symbols.

The script is idempotent in the "I haven't run it yet" sense (errors if
`tax_2026.db:positions` already has any `rollover_from_2025` rows). User
re-runs `--force` to overwrite if reconcile changes upstream.

## Migration phases

Each phase is one commit on `feature/sqlite-migration`. App is functional at
the end of each phase.

**Phase 1 — read-only SQLite under a compat layer.**
- Add `storage/`, schema files, `scripts/migrate_to_sqlite.py`.
- Implement `load_state()` shim that reads from SQLite and assembles the same
  dict shape the existing code expects.
- `save_state()` still writes JSON (so master and this branch can both work
  with the same data while in development).
- App behavior unchanged. Run side-by-side test: take a saved `state.json`,
  migrate, compare repro output of Form 8949 with both paths.

**Phase 2 — write-through.**
- Replace `save_state` with per-mutation writes through the repo.
- Routes still call `load_state()` to get the dict; the dict is now derived
  from SQLite each request, but only loads what the route reads.
- Add request-scoped connection in `before_request`.

**Phase 3 — drop the compat dict.**
- Routes call repo functions directly. Delete `load_state`/`save_state` and
  the compat shim.
- Run "2025" rename pass.

**Phase 4 — multi-year.**
- Add active-year selection (env var + UI dropdown for read-only view of
  prior year archives).
- Implement `scripts/rollover_year.py`.
- Create `tax_2026.db` from rollover.

**Phase 5 — cleanup.**
- Delete `state.json` from `data/` (keep a snapshot in `data/backups/`).
- Update README with the new workflow.
- Merge `--no-ff` into `master`.

## Decisions

1. **Per-year DBs are strict.** The importer reads the active year's DB and
   ingests only rows whose date falls within the active year. Rows outside
   the active year are silently skipped, with the count surfaced in the UI
   ("Imported N txs, skipped M outside 2026"). To get those skipped rows
   into the right DB, the user uploads the same CSV against that year.

2. **Prior years are read-only after rollover.** Once
   `rollover_year 2025 2026` has run, `tax_2025.db` is treated as a frozen
   archive. To fix a 2025 classification, the user explicitly unlocks
   2025, edits, and re-runs `rollover_year --force`. The `--force` flag
   replaces all `source = 'rollover_from_2025'` rows in `tax_2026.db:positions`.
   Rationale: by April 2026 the 2025 return is filed; touching 2025
   classifications implies an IRS amendment, not a casual edit. The
   deliberate unlock step matches the seriousness, and prevents silent
   shifts in 2026 numbers.

3. **Reports include a year selector.** UI dropdown picks which year's DB
   to render reports from. Active year is default; archived years render
   read-only.

## Other risks

1. **`failed_tokens` set in `fill_missing_usd_values`.** Currently
   reconstructed from `price_cache` keys per call. With SQLite, simpler:
   add a real `failed_lookups` table (in schema above).

2. **`uploads/`.** No change. Still gitignored, still ephemeral file landing
   pad. We could store raw CSV bytes in the DB for true audit trail, but
   that's out of scope here.

3. **JSON columns vs. normalized columns.** `transactions.parsed_details` and
   `reconciliations.lots_used` stay as JSON blobs (TEXT). They have variable
   shape (per-leg lists of varying lengths) and the app already treats them
   as opaque structured data. Normalizing them buys nothing for the current
   query patterns and would balloon table count. If a future analytics need
   shows up, we can virtual-column them out.

4. **Concurrent writers.** SQLite WAL allows many readers + one writer. The
   per-file RLock in `db.py` makes this single-writer-per-DB-from-our-process.
   That matches the current `RLock` around `save_state`. No regression.
