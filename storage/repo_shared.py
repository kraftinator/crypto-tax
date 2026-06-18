"""Cross-year DB accessors (shared.db).

Currently just price_cache. known_addresses lives on the per-year DB for
now since edits happen in the context of a year's workflow.
"""


def get_price(conn, cache_key):
    """Return the cached price for cache_key, or None if absent.

    Note: a stored value of None means 'lookup attempted, no price found' —
    so callers should check membership with `cache_key in list_prices()`
    if they need to distinguish 'not tried' from 'tried, no price'.
    """
    row = conn.execute(
        "SELECT price FROM price_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    return row['price'] if row else None


def has_price(conn, cache_key):
    row = conn.execute(
        "SELECT 1 FROM price_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    return row is not None


def set_price(conn, cache_key, price):
    conn.execute(
        "INSERT OR REPLACE INTO price_cache(cache_key, price) VALUES (?, ?)",
        (cache_key, price),
    )


def list_prices(conn):
    """Return {cache_key: price} for every cache entry (price may be None)."""
    return {
        r['cache_key']: r['price']
        for r in conn.execute("SELECT cache_key, price FROM price_cache")
    }


def list_failed_lookups(conn):
    """Return the set of cache_keys whose stored price is None."""
    return {
        r['cache_key']
        for r in conn.execute("SELECT cache_key FROM price_cache WHERE price IS NULL")
    }
