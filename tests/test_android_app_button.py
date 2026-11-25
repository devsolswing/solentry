"""Login screen Android app download button (2026-11-25).

James: a button on the login screen with the Android logo + "SolEntry" +
"Download app", linking to the APK at
https://raw.githubusercontent.com/devsolswing/solentry/main/static/solentry.apk
The APK itself is uploaded to static/solentry.apk via the GitHub web UI —
binary files cannot go through the push-files CLI — so until that upload
lands, the link 404s; the button is correct, the file is the missing part.
"""

import pathlib
import tempfile
import unittest

from helpers import make_client

APK_URL = (
    "https://raw.githubusercontent.com/devsolswing/solentry/main/static/solentry.apk"
)
TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "tradingbot"
    / "templates"
    / "login.html"
)


class TestAndroidAppButtonTemplate(unittest.TestCase):
    def test_login_template_links_the_apk(self):
        self.assertIn(APK_URL, TEMPLATE.read_text(encoding="utf-8"))

    def test_login_template_names_the_app_and_the_download(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("SolEntry", src)
        self.assertIn("Download app", src)

    def test_login_template_has_android_logo(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("fox-android-ico", src)
        self.assertIn("<svg", src)
        self.assertIn('viewBox="0 0 24 24"', src)

    def test_login_template_button_has_android_class(self):
        self.assertIn("fox-btn-android", TEMPLATE.read_text(encoding="utf-8"))


class TestAndroidAppButtonLive(unittest.TestCase):
    def test_login_page_renders_the_download_button(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, client = make_client(tmpdir)
            resp = client.get("/login")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn(APK_URL, html)
            self.assertIn("Download app", html)
            self.assertIn("fox-btn-android", html)


if __name__ == "__main__":
    unittest.main()
