"""Run with python3 -m unittest discover -s tests -p 'test_bandwagon_traffic.py'."""
import io
import json
import os
import socket
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.request import urlopen

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
SCRIPT = INSTALL.read_text()
SOURCE = SCRIPT.split("<<'BWG_PY'", 1)[1].split("\n", 1)[1].split("\nBWG_PY\n", 1)[0]


def load_updater(root):
    module = types.ModuleType("bwg_updater")
    module.__file__ = str(root / "update.py")
    exec(compile(SOURCE, str(INSTALL) + ":BWG_PY", "exec"), module.__dict__)
    return module


def usage(used=120, total=500, multiplier=1):
    return {"error": 0, "data_counter": used, "plan_monthly_data": total,
            "monthly_data_multiplier": multiplier, "data_next_reset": 1800000000}


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "nginx").mkdir()
        self.app = load_updater(self.root)
        self.site = self.root / "subscribe.conf"
        self.site.write_text(
            'server {\n location ~ ^/s/(clashMeta|clashMetaProfiles)/(.*) {\n'
            '  alias /etc/v2ray-agent/subscribe/$1/$2;\n }\n}\n')
        self.original = self.site.read_bytes()
        self.run = patch.object(self.app.subprocess, "run").start()
        self.fetch = patch.object(self.app, "fetch_header",
                                  return_value=self.app.traffic_header(usage())).start()
        self.addCleanup(patch.stopall)

    def enable(self):
        self.app.configure("123", str(self.site), "private-api-key")

    def test_multiplier_applies_to_both_and_no_expiry(self):
        header = self.app.traffic_header(usage("120", "500", "0.5"))
        self.assertIn(b"upload=0; download=60; total=250", header)
        self.assertNotIn(b"expire", header)
        self.assertNotIn(b"1800000000", header)

    def test_zero_and_exceeded_usage_are_valid(self):
        self.assertIn(b"download=0;", self.app.traffic_header(usage(0)))
        self.assertIn(b"download=600;", self.app.traffic_header(usage(600)))

    def test_invalid_api_data_never_becomes_zero(self):
        for field, value in (("error", 1), ("plan_monthly_data", 0),
                             ("data_counter", -1), ("monthly_data_multiplier", 0),
                             ("monthly_data_multiplier", "NaN"),
                             ("data_counter", "Infinity")):
            with self.subTest(field=field, value=value):
                info = usage()
                info[field] = value
                with self.assertRaises(ValueError):
                    self.app.traffic_header(info)
        with self.assertRaises(KeyError):
            self.app.traffic_header({"error": 0})

    def test_existing_install_and_repeated_enable(self):
        self.enable()
        self.enable()
        self.assertEqual(self.site.read_text().count(self.app.INCLUDE), 1)
        self.assertEqual(self.app.CONFIG.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"private-api-key", self.app.HEADER.read_bytes())
        self.assertNotIn(b"private-api-key", self.site.read_bytes())

    def test_bad_credentials_do_not_modify_existing_setup(self):
        self.enable()
        before = self.app.CONFIG.read_bytes()
        self.fetch.side_effect = ValueError("rejected")
        with self.assertRaises(ValueError):
            self.app.configure("456", str(self.site), "wrong")
        self.assertEqual(before, self.app.CONFIG.read_bytes())

    def test_unrecognized_nginx_layout_is_not_rewritten(self):
        self.site.write_text("server { location / { } }")
        original = self.site.read_bytes()
        with self.assertRaises(ValueError):
            self.enable()
        self.assertEqual(original, self.site.read_bytes())
        self.assertFalse(self.app.CONFIG.exists())

    def test_nginx_test_failure_rolls_back_config_and_credentials(self):
        self.run.side_effect = subprocess.CalledProcessError(1, "nginx")
        with self.assertRaises(subprocess.CalledProcessError):
            self.enable()
        self.assertEqual(self.site.read_bytes(), self.original)
        self.assertFalse(self.app.CONFIG.exists())
        self.assertFalse(self.app.HEADER.exists())

    def test_reload_failure_restores_previous_counter(self):
        self.enable()
        before = self.app.HEADER.read_bytes()
        self.fetch.return_value = self.app.traffic_header(usage(200))
        self.run.side_effect = [None, subprocess.CalledProcessError(1, "nginx")]
        with self.assertRaises(subprocess.CalledProcessError):
            self.app.update()
        self.assertEqual(self.app.HEADER.read_bytes(), before)

    def test_unchanged_usage_refreshes_freshness_without_reload(self):
        self.enable()
        old = time.time() - 1000
        os.utime(self.app.HEADER, (old, old))
        self.run.reset_mock()
        self.app.update()
        self.run.assert_not_called()
        self.assertGreater(self.app.HEADER.stat().st_mtime, old + 900)

    def test_monthly_reset_accepts_lower_counter(self):
        self.enable()
        self.fetch.return_value = self.app.traffic_header(usage(0))
        self.app.update()
        self.assertIn(b"download=0;", self.app.HEADER.read_bytes())

    def test_temporary_api_failure_preserves_last_good_value(self):
        self.enable()
        before = self.app.HEADER.read_bytes()
        self.fetch.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.app.update()
        self.assertEqual(before, self.app.HEADER.read_bytes())

    def test_stale_failure_removes_header_but_retains_subscription(self):
        self.enable()
        before = self.site.read_bytes()
        old = time.time() - 901
        os.utime(self.app.HEADER, (old, old))
        self.fetch.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.app.update()
        self.assertFalse(self.app.HEADER.exists())
        self.assertEqual(before, self.site.read_bytes())
        self.assertTrue(self.app.CONFIG.exists())
        self.fetch.side_effect = None
        self.app.update()
        self.assertTrue(self.app.HEADER.exists())

    def test_disable_is_repeatable_and_removes_credentials(self):
        self.enable()
        self.app.disable()
        self.app.disable()
        self.assertFalse(self.app.CONFIG.exists())
        self.assertFalse(self.app.HEADER.exists())
        self.assertIn(self.app.INCLUDE, self.site.read_text())

    def test_missing_subscription_does_not_query_api(self):
        self.enable()
        self.site.unlink()
        self.fetch.reset_mock()
        self.app.update()
        self.fetch.assert_not_called()

    def test_api_key_is_posted_only_to_kiwivm(self):
        real_app = load_updater(self.root)
        with patch.object(real_app, "urlopen") as request:
            request.return_value.__enter__.return_value = io.BytesIO(
                json.dumps(usage()).encode())
            result = real_app.fetch_header({"veid": "123", "api_key": "a&b"})
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "https://api.64clouds.com/v1/getServiceInfo")
            self.assertEqual(sent.get_method(), "POST")
            self.assertEqual(parse_qs(sent.data.decode())["api_key"], ["a&b"])
            self.assertIn(b"download=120;", result)

    def test_overwritten_subscription_config_is_reported(self):
        self.enable()
        self.site.write_bytes(self.original)
        self.fetch.reset_mock()
        with self.assertRaises(ValueError):
            self.app.update()
        self.fetch.assert_not_called()

    def test_errors_do_not_log_api_credentials(self):
        self.enable()
        self.fetch.side_effect = OSError("https://example.com/?api_key=private-api-key")
        with patch.object(self.app.sys, "argv", ["update.py", "update"]), \
             patch.object(self.app.sys, "stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.app.main(), 1)
            self.assertIn("OSError", stderr.getvalue())
            self.assertNotIn("private-api-key", stderr.getvalue())
            self.assertNotIn("https://", stderr.getvalue())


@unittest.skipUnless(os.environ.get("BWG_TEST_NGINX"), "Set BWG_TEST_NGINX for real HTTP tests")
class NginxIntegrationTests(unittest.TestCase):
    def test_subscription_http_headers_and_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nginx").mkdir()
            (root / "logs").mkdir()
            app = load_updater(root)
            app.INCLUDE = "include " + str(root / "nginx" / "*.conf") + ";"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            body = b"proxies: []\n"
            for category in ("clashMeta", "clashMetaProfiles"):
                (root / category).mkdir()
                (root / category / "test-id").write_bytes(body)
            site = root / "subscribe.conf"
            site.write_text(
                f"server {{ listen 127.0.0.1:{port};\n"
                "location ~ ^/s/(clashMeta|clashMetaProfiles)/(.*) {\n"
                f"alias {root}/$1/$2;\n{app.INCLUDE}\n}} }}\n")
            main = root / "nginx.conf"
            main.write_text(
                f"pid {root}/nginx.pid;\nerror_log {root}/error.log;\n"
                f"events {{ }}\nhttp {{ access_log off; include {site}; }}\n")
            base = [os.environ["BWG_TEST_NGINX"], "-p", str(root), "-c", str(main)]
            run = subprocess.run
            started = run(base, capture_output=True)
            self.assertEqual(started.returncode, 0, started.stderr.decode())

            def nginx(command, **kwargs):
                return run(base + command[1:], **kwargs)

            def get(category, expected):
                deadline = time.monotonic() + 5
                while True:
                    with urlopen(f"http://127.0.0.1:{port}/s/{category}/test-id",
                                 timeout=2) as response:
                        content = response.read()
                        actual = response.headers.get("Subscription-Userinfo")
                        cache = response.headers.get("Cache-Control")
                    if actual == expected or time.monotonic() > deadline:
                        self.assertEqual(actual, expected)
                        self.assertEqual(content, body)
                        if expected:
                            self.assertEqual(cache, "private, no-store")
                        return
                    time.sleep(0.05)
            try:
                get("clashMetaProfiles", None)
                with patch.object(app.subprocess, "run", side_effect=nginx), \
                     patch.object(app, "fetch_header",
                                  return_value=app.traffic_header(usage())):
                    app.configure("123", str(site), "private-api-key")
                    for category in ("clashMetaProfiles", "clashMeta"):
                        get(category, "upload=0; download=120; total=500")
                    app.disable()
                    get("clashMetaProfiles", None)
                self.assertNotIn("private-api-key", (root / "error.log").read_text())
            finally:
                run(base + ["-s", "quit"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
