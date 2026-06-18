"""Route smoke tests — every read-only route still responds.

Run from project root: `python -m unittest tests.test_route_smoke`

These don't validate correctness of the rendered content (the golden
snapshot tests do that). They just exercise each GET route through Flask's
test client and assert the response code isn't a 4xx/5xx. This is the gap
the goldens don't cover — template variables, route plumbing, dict-shape
mismatches that surface at render time but not at compute time.
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

# Flip Flask out of debug before importing app so the test client doesn't
# spin up the Werkzeug reloader.
os.environ.setdefault('FLASK_ENV', 'production')

from app import app, load_state  # noqa: E402


OK_CODES = {200, 302, 303, 304}


def _first_reconcilable_wallet_tx(state):
    """Find any (wallet_id, tx_index) that already has a reconciliation —
    guarantees the /reconcile/<wid>/<idx> route has a real target."""
    for recon_key in state.get('reconciliations', {}):
        parts = recon_key.split('_')
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[0], int(parts[1])
    return None


class RouteSmokeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.client = app.test_client()
        cls.state = load_state()
        if not cls.state:
            raise unittest.SkipTest("No state — nothing to smoke-test.")
        cls.first_wid = cls.state['wallets'][0]['id'] if cls.state.get('wallets') else None
        cls.recon_target = _first_reconcilable_wallet_tx(cls.state)

    def _assert_ok(self, path, expected_codes=OK_CODES):
        resp = self.client.get(path)
        self.assertIn(
            resp.status_code, expected_codes,
            f"GET {path} returned {resp.status_code} (body: {resp.data[:200]!r})",
        )

    # ---- core view routes -------------------------------------------------

    def test_index(self):           self._assert_ok('/')
    def test_positions(self):       self._assert_ok('/positions')
    def test_api_positions(self):   self._assert_ok('/api/positions')
    def test_wallets(self):         self._assert_ok('/wallets')
    def test_reconcile(self):       self._assert_ok('/reconcile')
    def test_reports(self):         self._assert_ok('/reports')

    # ---- parameterized routes — need a real id ---------------------------

    def test_wallet_detail(self):
        if not self.first_wid:
            self.skipTest("no wallets")
        self._assert_ok(f'/wallets/{self.first_wid}')

    def test_reconcile_detail(self):
        if not self.recon_target:
            self.skipTest("no reconciliations")
        wid, tx_idx = self.recon_target
        self._assert_ok(f'/reconcile/{wid}/{tx_idx}')

    # ---- report exporters ------------------------------------------------
    # These actually serialize data; if they 500 we want to know.

    def test_form8949_csv(self):
        self._assert_ok('/reports/form8949-csv')

    def test_income_csv(self):
        self._assert_ok('/reports/income-csv')


if __name__ == '__main__':
    unittest.main()
