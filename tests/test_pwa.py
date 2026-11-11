"""PWA (Android installable app): manifest, service worker, head tags.

The manifest and service worker are served from the app root so the
worker controls the whole app scope and Android Chrome offers
"Install app". The worker caches static assets only — never pages,
never /api/* — so live prices and balances are never stale.
"""

import json
import tempfile
import unittest

from helpers import make_client


class TestPwa(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_serves_valid_json(self):
        resp = self.client.get("/manifest.json")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.get_data(as_text=True))
        self.assertEqual(data["name"], "SolEntry")
        self.assertEqual(data["short_name"], "SolEntry")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["start_url"], "/")
        # Theme matches the app's dark-red topbar; gold stays #ffd700.
        self.assertEqual(data["theme_color"], "#1a0805")
        self.assertEqual(data["background_color"], "#1a0805")
        icons = data["icons"]
        self.assertTrue(icons, "manifest needs at least one icon")
        self.assertTrue(any("fox-red-480.jpg" in i["src"] for i in icons))
        self.assertTrue(any("fox-gold-480.jpg" in i["src"] for i in icons))
        for icon in icons:
            self.assertIn("sizes", icon)
            self.assertIn("type", icon)

    def test_service_worker_serves_with_fetch_handler(self):
        resp = self.client.get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("addEventListener(\"fetch\"", body)
        self.assertIn("addEventListener(\"install\"", body)
        self.assertIn("addEventListener(\"activate\"", body)
        # Live data is never cached: /api/* and /ws/ pass through.
        self.assertIn('/api/', body)
        self.assertIn('/ws/', body)
        # Only static assets are cached.
        self.assertIn("/static/", body)
        self.assertIn("caches.open", body)

    def test_service_worker_cache_version(self):
        # The installed app caches the shell under a versioned name.
        # v3 (2026-11-11): the old v2 cache held the pre-Clyde /
        # pre-Security-bot shell, so the installed Android app kept
        # showing the old Page 6 preset list. A new version makes
        # the worker fetch the fresh shell on install, and the
        # activate handler deletes the old cache.
        resp = self.client.get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('"solentry-v3"', body)
        self.assertNotIn("solentry-v2", body)

    def test_base_template_has_pwa_tags(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('<link rel="manifest" href="/manifest.json">', html)
        self.assertIn('<meta name="theme-color" content="#1a0805">', html)
        self.assertIn('content="yes"', html)  # mobile-web-app-capable
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("/static/fox-red-480.jpg", html)
        self.assertIn('navigator.serviceWorker.register("/sw.js")', html)

    def test_manifest_and_sw_need_no_login(self):
        # Installability must work before sign-in: the login page is
        # the front door, and the manifest must be reachable there.
        self.assertEqual(self.client.get("/manifest.json").status_code, 200)
        self.assertEqual(self.client.get("/sw.js").status_code, 200)


if __name__ == "__main__":
    unittest.main()
