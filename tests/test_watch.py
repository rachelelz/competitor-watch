"""Offline tests for scripts/watch.py. Run from the repo root: python3 -m unittest discover tests"""
import contextlib
import http.server
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import watch  # noqa: E402

PAGE = """<html><head><title>Acme | Plans</title>
<meta name="description" content="Acme plans for teams.">
<meta property="og:title" content="Acme OG">
<meta property="og:description" content="Acme OG text">
</head><body>
<div class="cookie-banner">We use cookies. <button>Accept</button></div>
<nav><a href="/">Home</a><a class="btn" href="/x">Menu button</a></nav>
<h1>Plan work together</h1>
<h2>Fast boards</h2><h3>Shared views</h3>
<p>Acme keeps your team on one board. It takes a minute to start.</p>
<p>Every project gets its own page with tasks, files and a short history of decisions. Teammates see what changed since they last looked.</p>
<p>Reports show what shipped each week and what is still open, so meetings can be shorter.</p>
<a class="btn-primary" href="/s">Start free</a><button>Book a demo</button>
<div class="plan"><h3>Starter</h3><p>$9 per user / month</p></div>
<div class="plan"><h3>Business</h3><p>&euro;29/mo</p></div>
<blockquote>Rotating quote that should not count here.</blockquote>
<footer>Footer words that should not count. &copy; 2026</footer>
</body></html>"""


def snapshot(html, url="https://acme.example/", status=200, final_url=None, error=None, today="2026-11-23"):
    fetched = {"status": status, "final_url": final_url or url, "html": html, "error": error}
    return watch.build_snapshot(url, fetched, today)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.f = watch.extract_fields(PAGE)

    def test_head_fields(self):
        self.assertEqual(self.f["title"], "Acme | Plans")
        self.assertEqual(self.f["meta_description"], "Acme plans for teams.")
        self.assertEqual(self.f["og_title"], "Acme OG")
        self.assertEqual(self.f["og_description"], "Acme OG text")

    def test_headings_and_ctas(self):
        self.assertEqual(self.f["h1"], ["Plan work together"])
        self.assertEqual(self.f["h2h3"], ["Fast boards", "Shared views"])
        self.assertEqual(self.f["ctas"], ["Start free", "Book a demo"])  # nav and cookie buttons are skipped

    def test_plan_names_stay_out_of_headings(self):
        self.assertEqual(self.f["plans"], ["Starter", "Business"])
        self.assertNotIn("Starter", self.f["h2h3"])

    def test_body_text_skips_nav_footer_quotes_and_cookies(self):
        text = " ".join(self.f["sentences"])
        self.assertIn("Acme keeps your team on one board.", text)
        for noise in ("Footer words", "Rotating quote", "We use cookies", "Menu button"):
            self.assertNotIn(noise, text)

    def test_unclosed_paragraph_tags_do_not_break_parsing(self):
        fields = watch.extract_fields("<h1>Hello there</h1><p>One two three four.<p>Five six seven eight.")
        self.assertEqual(fields["h1"], ["Hello there"])
        self.assertEqual(len(fields["sentences"]), 2)


class PricingTests(unittest.TestCase):
    def test_price_strings(self):
        found = watch.find_prices("Pro is $12 per user / month. Team: $24 per user per month. Solo €9/mo. Add-on £5.50.")
        self.assertEqual(found, ["$12 per user/month", "$24 per user per month", "€9/mo", "£5.50"])

    def test_ignores_numbers_without_currency(self):
        self.assertEqual(watch.find_prices("Join 12,000 teams in 40 countries."), [])

    def test_price_change_is_pricing_signal(self):
        old = snapshot(PAGE)
        new = snapshot(PAGE.replace("$9 per user", "$11 per user"))
        changes, _ = watch.diff_snapshots(old, new)
        self.assertEqual([c["signal"] for c in changes], ["pricing"])
        self.assertIn('"$9 per user/month" to "$11 per user/month"', changes[0]["summary"])


class NoiseTests(unittest.TestCase):
    def test_dates_and_numbers_are_noise(self):
        for text in ("Last updated: November 16, 2026", "Updated 12 Nov 2026", "2026", "Copyright 2026. All rights reserved."):
            self.assertTrue(watch.is_noise(text), text)

    def test_bare_price_is_noise_for_copy(self):
        self.assertTrue(watch.is_noise("$15 per user / month"))

    def test_real_sentences_are_not_noise(self):
        self.assertFalse(watch.is_noise("Plan sprints in two minutes."))
        self.assertFalse(watch.is_noise("Join 12,400 teams today."))

    def test_ignore_regex(self):
        self.assertTrue(watch.is_noise("Join 12,400 teams today.", [r"Join [\d,]+ teams"]))

    def test_noise_does_not_trigger_changes(self):
        old = snapshot(PAGE.replace("</body>", "<p>Last updated: November 9, 2026</p></body>"))
        new = snapshot(PAGE.replace("</body>", "<p>Last updated: November 16, 2026</p></body>")
                       .replace("Rotating quote that", "A different quote that").replace("We use cookies.", "Cookies help us."))
        changes, notes = watch.diff_snapshots(old, new)
        self.assertEqual((changes, notes), ([], []))


class DiffSignalTests(unittest.TestCase):
    def diff(self, old_html, new_html, **kw):
        return watch.diff_snapshots(snapshot(old_html), snapshot(new_html), **kw)[0]

    def test_h1_is_positioning_and_ranks_first(self):
        changes = self.diff(PAGE, PAGE.replace("Plan work together", "Plan work for agencies").replace("Start free", "Talk to sales"))
        self.assertEqual(changes[0]["signal"], "positioning")
        self.assertIn("headline (H1)", changes[0]["summary"])
        self.assertEqual(len(changes), 2)

    def test_cta_change(self):
        changes = self.diff(PAGE, PAGE.replace("Book a demo", "Get a quote"))
        self.assertEqual(changes[0]["signal"], "positioning")
        self.assertIn('"Book a demo" to "Get a quote"', changes[0]["summary"])

    def test_plan_rename_is_pricing(self):
        changes = self.diff(PAGE, PAGE.replace("Business", "Agency"))
        self.assertEqual([(c["signal"]) for c in changes], ["pricing"])

    def test_body_sentence_is_copy(self):
        changes = self.diff(PAGE, PAGE.replace("It takes a minute to start.", "Setup takes under five minutes."))
        self.assertEqual([c["signal"] for c in changes], ["copy"])

    def test_status_change_is_technical(self):
        changes, _ = watch.diff_snapshots(snapshot(PAGE), snapshot("<h1>Gone</h1>", status=404))
        self.assertEqual(changes[0]["signal"], "technical")
        self.assertIn("HTTP 404", changes[0]["summary"])

    def test_new_redirect_is_technical(self):
        new = snapshot(PAGE, final_url="https://acme.example/new-home")
        changes, _ = watch.diff_snapshots(snapshot(PAGE), new)
        self.assertEqual(changes[0]["signal"], "technical")
        self.assertIn("redirects to https://acme.example/new-home", changes[0]["summary"])

    def test_trailing_slash_is_not_a_redirect(self):
        new = snapshot(PAGE, url="https://acme.example/pricing", final_url="https://acme.example/pricing/")
        self.assertEqual(watch.technical_changes(None, new), [])

    def test_fetch_failure_is_technical(self):
        changes, _ = watch.diff_snapshots(snapshot(PAGE), snapshot("", status=None, error="timed out"))
        self.assertIn("could not be fetched", changes[0]["summary"])

    def test_thin_page_is_reported_not_diffed(self):
        thin = "<html><body><div id='root'>Loading</div></body></html>"
        changes, notes = watch.diff_snapshots(snapshot(PAGE), snapshot(thin))
        self.assertEqual(changes, [])
        self.assertIn("Very little visible text", notes[0])

    def test_user_ignore_list_hides_a_rotating_heading(self):
        spring = PAGE.replace("Fast boards", "Spring promo boards")
        summer = PAGE.replace("Fast boards", "Summer promo boards")
        self.assertEqual(len(self.diff(spring, summer)), 1)
        self.assertEqual(self.diff(spring, summer, ignore=[r"promo"]), [])


class RobotsTests(unittest.TestCase):
    class FakeParser:
        def parse(self, lines):
            self.lines = lines

        def can_fetch(self, agent, url):
            return "/private" not in url

    def checker(self, status=200, body="User-agent: *\nDisallow: /private"):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return {"status": status, "final_url": url, "html": body, "error": None}
        return watch.RobotsChecker(fake_fetch, self.FakeParser), calls

    def test_disallowed_url_is_skipped_with_a_note(self):
        robots, _ = self.checker()
        fetch = watch.live_fetcher(robots)
        with tempfile.TemporaryDirectory() as state:
            page = watch.check_page("https://acme.example/private/plans", [], state, "2026-11-23", fetch)
            self.assertEqual(page["status"], "skipped")
            self.assertIn("robots.txt disallows", page["notes"][0])
            self.assertEqual(list(Path(state).iterdir()), [])  # nothing saved

    def test_robots_is_fetched_once_per_site(self):
        robots, calls = self.checker()
        robots.allowed("https://acme.example/a")
        robots.allowed("https://acme.example/b")
        self.assertEqual(calls, ["https://acme.example/robots.txt"])

    def test_missing_robots_allows_and_403_denies(self):
        self.assertTrue(self.checker(status=404, body="")[0].allowed("https://acme.example/private"))
        self.assertFalse(self.checker(status=403, body="")[0].allowed("https://acme.example/a"))


class SnapshotStorageTests(unittest.TestCase):
    def test_keeps_only_the_last_n_and_ignores_todays_file_as_previous(self):
        with tempfile.TemporaryDirectory() as state:
            for day in range(1, 6):
                watch.save_snapshot(state, "acme", snapshot(PAGE, today=f"2026-11-0{day}"), keep=3)
            names = sorted(p.name for p in (Path(state) / "acme").iterdir())
            self.assertEqual(names, ["2026-11-03.json", "2026-11-04.json", "2026-11-05.json"])
            self.assertEqual(watch.load_previous(state, "acme", "2026-11-05")["date"], "2026-11-04")
            self.assertIsNone(watch.load_previous(state, "acme", "2026-11-03"))

    def test_slug(self):
        self.assertEqual(watch.slug_for_url("https://Tallyboard.example/"), "tallyboard.example")
        self.assertEqual(watch.slug_for_url("https://tallyboard.example/pricing/"), "tallyboard.example-pricing")


class LiveFetchTests(unittest.TestCase):
    """Fetch from a throwaway server on 127.0.0.1. No outside network."""

    def test_follows_redirect_and_reports_final_url_and_404(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/old":
                    self.send_response(301)
                    self.send_header("Location", "/new")
                    self.end_headers()
                elif self.path == "/new":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<h1>New page</h1>")
                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"missing")
                self.server.seen_agents.append(self.headers.get("User-Agent"))

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.seen_agents = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            moved = watch.fetch_url(base + "/old", timeout=5)
            missing = watch.fetch_url(base + "/nope", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((moved["status"], moved["final_url"]), (200, base + "/new"))
        self.assertEqual(missing["status"], 404)
        self.assertTrue(all(a == watch.USER_AGENT for a in server.seen_agents))

    def test_connection_failure_returns_error_not_exception(self):
        result = watch.fetch_url("http://127.0.0.1:9/", timeout=2)
        self.assertIsNone(result["status"])
        self.assertTrue(result["error"])


class PacingTests(unittest.TestCase):
    def test_waits_between_requests_but_not_before_the_first(self):
        config = {"competitors": [{"name": "A", "urls": ["https://a.example/", "https://a.example/b", "https://a.example/c"]}]}
        pauses = []
        fetch = lambda url: {"status": 200, "final_url": url, "html": PAGE, "error": None}
        with tempfile.TemporaryDirectory() as state:
            watch.run_watch(config, state, fetch, "2026-11-23", delay=2, sleep=pauses.append)
        self.assertEqual(pauses, [2, 2])


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.out = Path(cls.dir.name) / "report.md"
        cls.report = watch.run_demo(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_first_line_names_the_biggest_change(self):
        first = self.report.splitlines()[0]
        self.assertTrue(first.startswith("Look at this first: Tallyboard homepage"))
        self.assertIn("Project management for agencies", first)

    def test_expected_changes_are_flagged(self):
        for expected in ('"$12 per user/month"', '"$15 per user/month"', '"Book a demo"', "redirects to https://orbitask.example/product",
                         "returns HTTP 404"):
            self.assertIn(expected, self.report)

    def test_noise_only_page_is_listed_under_no_change(self):
        no_change = self.report.split("## No change")[1]
        self.assertIn("https://plankit.example/", no_change)
        self.assertNotIn("Marcus", self.report)
        self.assertNotIn("November 16", self.report)

    def test_report_is_written_and_has_no_em_dashes(self):
        self.assertEqual(self.out.read_text(encoding="utf-8"), self.report)
        self.assertNotIn("—", self.report)

    def test_first_run_says_nothing_to_compare(self):
        config = watch.load_config(ROOT / "examples" / "watchlist.example.json")
        with tempfile.TemporaryDirectory() as state:
            results = watch.run_watch(config, state, watch.dir_fetcher(ROOT / "examples" / "fixtures" / "week1"), "2026-11-16")
        self.assertIn("first run", watch.render_report(results, "2026-11-16").splitlines()[0])

    def test_from_dir_cli_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.md"
            args = ["--config", str(ROOT / "examples" / "watchlist.example.json"), "--state", tmp + "/state", "--out", str(out)]
            fixtures = ROOT / "examples" / "fixtures"
            watch.main(args + ["--from-dir", str(fixtures / "week1"), "--date", "2026-11-16"])
            watch.main(args + ["--from-dir", str(fixtures / "week2"), "--date", "2026-11-23"])
            self.assertTrue(out.read_text().startswith("Look at this first: Tallyboard homepage"))


class BadConfigTests(unittest.TestCase):
    def write_config(self, tmp, competitor):
        path = Path(tmp) / "watchlist.json"
        path.write_text(json.dumps({"competitors": [competitor]}), encoding="utf-8")
        return path

    def run_bad(self, competitor):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, competitor)
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()) as err:
                watch.load_config(path)
        return caught.exception.code, err.getvalue()

    def test_url_without_scheme_is_rejected_cleanly(self):
        code, message = self.run_bad({"name": "Acme", "urls": ["notaurl"]})
        self.assertEqual(code, 2)
        self.assertIn("Acme", message)
        self.assertIn("notaurl", message)

    def test_non_http_scheme_is_rejected(self):
        code, _ = self.run_bad({"name": "Acme", "urls": ["ftp://acme.example/"]})
        self.assertEqual(code, 2)

    def test_bad_regex_is_rejected_cleanly(self):
        code, message = self.run_bad({"name": "Acme", "urls": ["https://acme.example/"], "ignore": ["("]})
        self.assertEqual(code, 2)
        self.assertIn("Bad regex in ignore for Acme: (", message)

    def test_bad_url_at_fetch_time_becomes_a_note(self):
        def boom(url):
            raise ValueError("unknown url type")
        with tempfile.TemporaryDirectory() as state:
            for url in ("notaurl", "https://acme.example/"):
                page = watch.check_page(url, [], state, "2026-11-23", boom)
                self.assertEqual(page["status"], "skipped")
                self.assertTrue(page["notes"])


class WorkingFolderTests(unittest.TestCase):
    def test_script_uses_current_folder_not_repo_folder(self):
        fixtures = ROOT / "examples" / "fixtures" / "week1"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "pages").mkdir()
            for f in fixtures.glob("*.html"):
                (work / "pages" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            (work / "watchlist.json").write_text((ROOT / "examples" / "watchlist.example.json").read_text(), encoding="utf-8")
            cmd = [sys.executable, str(ROOT / "scripts" / "watch.py"), "--config", "watchlist.json", "--state", "state",
                   "--out", "report.md", "--from-dir", "pages", "--date", "2026-11-16"]
            done = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((work / "report.md").exists())
            self.assertTrue((work / "state").is_dir())


if __name__ == "__main__":
    unittest.main()
