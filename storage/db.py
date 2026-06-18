"""SQLite connection helpers and schema bootstrap.

The app talks to two SQLite databases:

  - data/tax_<YYYY>.db   year-scoped: wallets, transactions, classifications,
                         positions, reconciliations, meta
  - data/shared.db       cross-year: price_cache

Per-file RLocks serialize writes within this process, matching the
single-writer semantics of the previous JSON load/save loop.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), 'migrations')

_locks = {}
_lock_guard = threading.Lock()


def _lock_for(path):
    with _lock_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _locks[path] = lock
        return lock


def year_db_path(year):
    return os.path.join(DATA_DIR, f'tax_{year}.db')


def shared_db_path():
    return os.path.join(DATA_DIR, 'shared.db')


@contextmanager
def connect(path):
    """Open a sqlite3 connection with sensible pragmas, serialized per-file."""
    with _lock_for(path):
        # isolation_level=None puts us in autocommit; we issue explicit BEGIN /
        # COMMIT / ROLLBACK and Python's DB-API stays out of the way.
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
        finally:
            conn.close()


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def bootstrap_year_db(year):
    """Create tax_<year>.db with the initial schema if it does not exist."""
    path = year_db_path(year)
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect(path) as conn:
        if _user_version(conn) == 0:
            with open(os.path.join(MIGRATIONS_DIR, 'year_0001_initial.sql')) as f:
                conn.executescript(f.read())
    return path


def bootstrap_shared_db():
    path = shared_db_path()
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect(path) as conn:
        if _user_version(conn) == 0:
            with open(os.path.join(MIGRATIONS_DIR, 'shared_0001_initial.sql')) as f:
                conn.executescript(f.read())
    return path
