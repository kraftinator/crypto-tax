-- Initial schema for the cross-year shared DB (shared.db).
--
-- Holds data that's not tied to a single tax year: token prices (cached
-- per coingecko_id + date) and the live known_addresses lookup is on the
-- year DB for now (since it's small and editing happens within a year's
-- workflow). If/when known_addresses needs to be shared, move it here.

PRAGMA user_version = 1;

CREATE TABLE price_cache (
  cache_key TEXT PRIMARY KEY,        -- "{coingecko_id}_{YYYY-MM-DD}"
  price     REAL                     -- NULL = lookup attempted, no price found
);
