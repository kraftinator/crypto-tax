-- Initial schema for per-year tax DB (tax_YYYY.db).
--
-- Most state.json sections are stored as one row per logical entry with a
-- JSON `data` column. This trades the ability to do column-level queries
-- for an exact round-trip of the existing dict shape — which is what the
-- compat shim in load_state() needs.
--
-- The wallet_id + date columns on `transactions` are denormalized out of
-- the JSON purely so common filters (per-wallet, by-date) can be indexed
-- without touching JSON1.

PRAGMA user_version = 1;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Rows: tax_year, cost_basis_method, filename, known_addresses (JSON blob).

CREATE TABLE wallets (
  id          TEXT PRIMARY KEY,
  sort_order  INTEGER NOT NULL,
  data        TEXT NOT NULL          -- JSON of the full wallet object
);

CREATE TABLE transactions (
  wallet_id  TEXT    NOT NULL,
  tx_index   INTEGER NOT NULL,       -- position in the wallet's tx list
  date       TEXT,                   -- ISO 8601 from the tx (indexed)
  type       TEXT,                   -- TRADE / RECEIVE / SEND / MINT / BURN / ... (indexed)
  data       TEXT    NOT NULL,       -- JSON of the full tx object
  PRIMARY KEY (wallet_id, tx_index)
);
CREATE INDEX ix_tx_wallet_date ON transactions (wallet_id, date);
CREATE INDEX ix_tx_wallet_type ON transactions (wallet_id, type);

CREATE TABLE classifications (
  key   TEXT PRIMARY KEY,            -- "{wallet_id}_{tx_index}_{item_index}"
  value TEXT NOT NULL                -- Income / Airdrop / Transfer / Payment / ...
);

CREATE TABLE positions (
  pos_index INTEGER PRIMARY KEY,     -- preserves list order
  data      TEXT NOT NULL            -- JSON of the position object
);

CREATE TABLE reconciliations (
  recon_key  TEXT PRIMARY KEY,       -- "{wallet_id}_{tx_index}"
  wallet_id  TEXT NOT NULL,
  tx_index   INTEGER NOT NULL,
  data       TEXT NOT NULL           -- JSON of the recon object
);
CREATE INDEX ix_recon_wallet ON reconciliations (wallet_id);
