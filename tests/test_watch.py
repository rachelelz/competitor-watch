"""Offline tests for scripts/watch.py. Run from the repo root: python3 -m unittest discover tests"""
import ast
import contextlib
import http.server
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import watch  # noqa: E402

EXAMPLE_CONFIG = ROOT / "examples" / "watchlist.example.json"
FIXTURES = ROOT / "examples" / "fixtures"

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

# Same head as PAGE, almost no body text: what a JavaScript-built page looks like to the script.
THIN = PAGE.split("<body>")[0] + "<body><div id='root'>Loading</div></body></html>"


def snapshot(html, url="https://acme.example/", status=200, final_url=None, error=None, today="2026-11-23"):
    fetched = {"status": status, "final_url": final_url or url, "html": html, "error": error}
    return watch.build_snapshot(url, fetched, today)


def page_fetch(html=PAGE, status=200, final_url=None, error=None):
    """A fetcher that always returns the same result."""
    def fetch(url):
        return {"status": status, "final_url": final_url or url, "html": html, "error": error}
    return fetch


def check(state, day, url="https://acme.example/", ignore=(), keep=8, **fetched):
    """Run one page through check_page with a fake fetch result."""
    return watch.check_page(url, list(ignore), state, day, page_fetch(**fetched), keep)


def report_for(*pages, day="2026-11-23"):
    return watch.render_report([{"name": "Acme", "pages": list(pages)}], day)


@contextlib.contextmanager
def quiet():
    """Hide what the CLI prints."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


@contextlib.contextmanager
def local_server(routes):
    """Serve {path: (status, headers, body bytes)} on 127.0.0.1. No outside network."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.seen_agents.append(self.headers.get("User-Agent"))
            status, headers, body = routes.get(self.path, (404, {}, b"missing"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.seen_agents = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


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

    def test_svg_title_does_not_leak_into_page_title(self):
        logo = '<a href="/"><svg><title>Logo icon</title><path d=""/></svg></a>'
        cases = {
            f"<html><head><title>Acme | Plans</title></head><body>{logo}<h1>Plan work</h1></body></html>": "Acme | Plans",
            f"{logo}<title>Acme</title><h1>Plan work</h1>": "Acme",
            f"<body>{logo}<h1>Plan work</h1></body>": "",
        }
        for html, title in cases.items():
            with self.subTest(html=html):
                fields = watch.extract_fields(html)
                self.assertEqual(fields["title"], title)
                self.assertEqual(fields["h1"], ["Plan work"])

    def test_unclosed_p_and_li_do_not_pile_up(self):
        parser = watch.PageParser()
        parser.feed("<ul>" + "<li>Item with a few words" * 3000)
        self.assertEqual([e["tag"] for e in parser.stack], ["ul", "li"])
        parser.feed("</ul>" + "<p>Some words in a row." * 3000)
        self.assertEqual([e["tag"] for e in parser.stack], ["p"])
        nested = watch.extract_fields("<ul><li>Outer item here<ul><li>Inner item here<li>Second inner item</ul></ul>")
        self.assertEqual(nested["sentences"], ["Outer item here", "Inner item here", "Second inner item"])

    def test_carousel_and_accordion_buttons_are_not_ctas(self):
        html = ('<button aria-expanded="false">How does billing work?</button>'
                '<button class="swiper-button-next">Go on</button><button class="carousel-control-prev">Back</button>'
                '<button class="arrow">&rsaquo;</button><button>Previous</button><button aria-label="Next slide">Next</button>'
                '<a class="btn" href="/signup">Start free</a>')
        self.assertEqual(watch.extract_fields(html)["ctas"], ["Start free"])

    def test_more_plan_container_class_names(self):
        html = ('<section class="plans"><div class="card"><h3>Solo</h3></div></section>'
                '<div class="pricing-card"><h3>Studio</h3></div><div class="pricing-tier"><h4>Agency</h4></div>'
                '<h2>Why teams switch</h2>')
        fields = watch.extract_fields(html)
        self.assertEqual(fields["plans"], ["Solo", "Studio", "Agency"])
        self.assertEqual(fields["h2h3"], ["Why teams switch"])


class PricingTests(unittest.TestCase):
    def test_price_strings(self):
        found = watch.find_prices("Pro is $12 per user / month. Team: $24 per user per month. Solo €9/mo. Add-on £5.50.")
        self.assertEqual(found, ["$12 per user/month", "$24 per user per month", "€9/mo", "£5.50"])

    def test_more_price_formats(self):
        found = watch.find_prices("Solo 12 € per month. Team € 24/mo. Pro USD 49 per seat. Max 49 EUR. Step 2 $15.")
        self.assertEqual(found, ["12 € per month", "€ 24/mo", "USD 49 per seat", "49 EUR", "$15"])

    def test_ignores_numbers_without_currency(self):
        self.assertEqual(watch.find_prices("Join 12,000 teams in 40 countries. Euro 2024 and usd 5."), [])

    def test_price_change_is_pricing_signal(self):
        old = snapshot(PAGE)
        new = snapshot(PAGE.replace("$9 per user", "$11 per user"))
        changes, _ = watch.diff_snapshots(old, new)
        self.assertEqual([c["signal"] for c in changes], ["pricing"])
        self.assertIn('"$9 per user/month" to "$11 per user/month"', changes[0]["summary"])

    def test_price_inside_a_sentence_is_only_a_pricing_change(self):
        old = PAGE.replace("</body>", "<p>Pro costs $12 per user / month and includes everything.</p></body>")
        new = old.replace("$12 per user", "$15 per user")
        changes, _ = watch.diff_snapshots(snapshot(old), snapshot(new))
        self.assertEqual([c["signal"] for c in changes], ["pricing"])
        reworded = new.replace("includes everything", "includes every feature")
        changes, _ = watch.diff_snapshots(snapshot(old), snapshot(reworded))
        self.assertEqual(sorted(c["signal"] for c in changes), ["copy", "pricing"])


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

    def test_copy_summary_uses_singular_and_plural(self):
        one = watch.copy_change([], ["Setup takes under five minutes."], [])
        self.assertIn("(1 sentence added, 0 removed)", one["summary"])
        words = ["one", "two", "three", "four", "five", "six", "seven", "eight"]
        seven = watch.copy_change([], [f"Sentence number {w} is here." for w in words[:7]], [])
        self.assertEqual(seven["details"][-1], "1 more added sentence not shown")
        eight = watch.copy_change([f"Sentence number {w} is here." for w in words], [], [])
        self.assertIn("(0 sentences added, 8 removed)", eight["summary"])
        self.assertEqual(eight["details"][-1], "2 more removed sentences not shown")

    def test_new_redirect_is_technical(self):
        new = snapshot(PAGE, final_url="https://acme.example/new-home")
        changes, _ = watch.diff_snapshots(snapshot(PAGE), new)
        self.assertEqual(changes[0]["signal"], "technical")
        self.assertIn("redirects to https://acme.example/new-home", changes[0]["summary"])

    def test_trailing_slash_is_not_a_redirect(self):
        new = snapshot(PAGE, url="https://acme.example/pricing", final_url="https://acme.example/pricing/")
        self.assertEqual(watch.diff_snapshots(None, new), ([], []))

    def test_thin_page_gets_a_note_and_no_body_diff(self):
        changes, notes = watch.diff_snapshots(snapshot(PAGE), snapshot(THIN))
        self.assertEqual(changes, [])
        self.assertIn("Very little visible text", notes[0])

    def test_thin_page_note_shows_on_the_first_run(self):
        with tempfile.TemporaryDirectory() as state:
            page = check(state, "2026-11-16", html=THIN)
        self.assertEqual(page["status"], "baseline")
        self.assertIn(watch.THIN_NOTE, page["notes"])
        self.assertIn("Very little visible text", report_for(page, day="2026-11-16"))

    def test_thin_page_still_compares_title_meta_and_og(self):
        new = THIN.replace("Acme | Plans", "Acme | Plans for agencies").replace("Acme OG text", "Acme OG for agencies")
        changes, notes = watch.diff_snapshots(snapshot(THIN), snapshot(new))
        self.assertEqual(sorted(c["title"] for c in changes), ["page title changed", "social (og) description changed"])
        self.assertIn(watch.THIN_NOTE, notes)

    def test_user_ignore_list_hides_a_rotating_heading(self):
        spring = PAGE.replace("Fast boards", "Spring promo boards")
        summer = PAGE.replace("Fast boards", "Summer promo boards")
        self.assertEqual(len(self.diff(spring, summer)), 1)
        self.assertEqual(self.diff(spring, summer, ignore=[r"promo"]), [])

    def test_ignore_applies_to_h1_title_meta_and_og(self):
        def promo(season):
            return (PAGE.replace("Plan work together", f"{season} promo: plan work").replace("Acme | Plans", f"Acme | {season} promo")
                    .replace("Acme plans for teams.", f"{season} promo for teams.").replace("Acme OG", f"{season} promo OG"))
        labels = sorted(c["title"] for c in self.diff(promo("Spring"), promo("Summer")))
        self.assertEqual(labels, ["headline (H1) changed", "meta description changed", "page title changed",
                                  "social (og) description changed", "social (og) title changed"])
        self.assertEqual(self.diff(promo("Spring"), promo("Summer"), ignore=["promo"]), [])

    def test_ignore_never_hides_prices_or_plan_names(self):
        new = PAGE.replace("Business", "Agency").replace("$9 per user", "$11 per user")
        changes = self.diff(PAGE, new, ignore=["Business", "Agency", r"\$"])
        self.assertEqual(sorted(c["title"] for c in changes), ["plan names changed", "prices changed"])


class ProblemTests(unittest.TestCase):
    def test_new_404_leads_the_report_and_is_counted_as_a_problem(self):
        with tempfile.TemporaryDirectory() as state:
            check(state, "2026-11-16")
            page = check(state, "2026-11-23", status=404, html="<h1>Gone</h1>")
        self.assertEqual(page["status"], "problem")
        report = report_for(page)
        self.assertTrue(report.startswith("Look at this first: Acme homepage, the page now returns HTTP 404."))
        self.assertIn("HTTP 404, first seen 2026-11-23. Last snapshot with page content: 2026-11-16.", report)
        self.assertIn("With changes: 0. Problems: 1.", report)

    def test_persistent_404_is_reported_every_run_with_first_seen_date(self):
        about, weeks = "https://acme.example/about", {}
        with tempfile.TemporaryDirectory() as state:
            for day, status in (("2026-11-09", 200), ("2026-11-16", 404), ("2026-11-23", 404), ("2026-11-30", 404)):
                broken = check(state, day, status=status, html=PAGE if status == 200 else "missing")
                weeks[day] = (broken, check(state, day, url=about))
        for day in ("2026-11-23", "2026-11-30"):
            report = report_for(*weeks[day], day=day)
            self.assertIn("(https://acme.example/): still HTTP 404, first seen 2026-11-16.", report.split("## Problems")[1])
            self.assertEqual(report.split("## No change")[1].strip(), f"- Acme /about: {about}")
            self.assertIn("With changes: 0. Problems: 1. No change: 1.", report)
            self.assertEqual(report.splitlines()[0],
                             "Look at this first: nothing changed. 1 page could not be read, see Problems below.")

    def test_first_seen_404_is_a_problem_not_a_change(self):
        with tempfile.TemporaryDirectory() as state:
            page = check(state, "2026-11-23", status=404, html="missing")
        report = report_for(page)
        self.assertNotIn("Page status changed", report)
        self.assertIn("HTTP 404, first seen 2026-11-23 on the first check of this page", report)
        self.assertIn("With changes: 0. Problems: 1.", report)

    def test_fetch_failure_says_couldnt_reach_in_plain_words(self):
        raw = "[Errno 8] nodename nor servname provided, or not known"
        with tempfile.TemporaryDirectory() as state:
            check(state, "2026-11-16")
            week2 = check(state, "2026-11-23", status=None, html="", error=raw)
            week3 = check(state, "2026-11-30", status=None, html="", error="timed out")
        report = report_for(week2)
        self.assertIn("couldn't reach acme.example (DNS lookup failed)", report)
        self.assertNotIn("Errno", report)
        self.assertIn("still couldn't reach acme.example (timed out), first seen 2026-11-23", report_for(week3, day="2026-11-30"))


class HistoryTests(unittest.TestCase):
    NEW_H1 = PAGE.replace("Plan work together", "Plan work for agencies")

    def test_compares_with_last_good_snapshot_after_a_failed_week(self):
        with tempfile.TemporaryDirectory() as state:
            check(state, "2026-11-09")
            check(state, "2026-11-16", status=None, html="", error="timed out")
            page = check(state, "2026-11-23", html=self.NEW_H1)
        self.assertEqual(page["previous"], "2026-11-09")
        summaries = " ".join(c["summary"] for c in page["changes"])
        self.assertIn("headline (H1) changed", summaries)
        self.assertIn("the page can be read again", summaries)
        report = report_for(page)
        self.assertIn("https://acme.example/ (compared with 2026-11-09)", report)
        self.assertIn("Compared with 2026-11-09, the last snapshot with page content.", report)

    def test_small_keep_never_deletes_the_last_good_snapshot(self):
        with tempfile.TemporaryDirectory() as state:
            folder = Path(state) / "acme.example"
            check(state, "2026-11-09", keep=1)
            check(state, "2026-11-16", keep=1, status=503, html="busy")
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["2026-11-09.json", "2026-11-16.json"])
            page = check(state, "2026-11-23", keep=1, html=self.NEW_H1)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["2026-11-23.json"])
        self.assertEqual(page["previous"], "2026-11-09")
        self.assertIn("headline (H1) changed", " ".join(c["summary"] for c in page["changes"]))

    def test_damaged_snapshot_is_skipped_with_a_note(self):
        half = '{"fields": {"title": "Acme"}, "final_url": "https://acme.example/", "status": 200}'
        for bad in ("", "{not json", "[]", '{"fields": "nope"}', half):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as state:
                check(state, "2026-11-09")
                (Path(state) / "acme.example" / "2026-11-16.json").write_text(bad, encoding="utf-8")
                page = check(state, "2026-11-23", html=PAGE.replace("Book a demo", "Get a quote"))
                self.assertEqual((page["status"], page["previous"]), ("changed", "2026-11-09"))
                self.assertTrue(any("damaged snapshot file acme.example/2026-11-16.json" in n for n in page["notes"]))
                self.assertIn("## Notes", report_for(page))


class RedirectTests(unittest.TestCase):
    def test_redirect_on_first_snapshot_is_a_note(self):
        with tempfile.TemporaryDirectory() as state:
            page = check(state, "2026-11-16", url="http://acme.example/", final_url="https://www.acme.example/")
        self.assertEqual((page["status"], page["changes"]), ("baseline", []))
        self.assertIn("This page redirects to https://www.acme.example/.", " ".join(page["notes"]))
        self.assertIn("first run", report_for(page, day="2026-11-16").splitlines()[0])

    def test_same_redirect_is_quiet_and_a_new_final_url_is_flagged(self):
        url, www = "http://acme.example/", "https://www.acme.example/"
        with tempfile.TemporaryDirectory() as state:
            check(state, "2026-11-09", url=url, final_url=www)
            same = check(state, "2026-11-16", url=url, final_url=www)
            moved = check(state, "2026-11-23", url=url, final_url=www + "en/")
        self.assertEqual(same["status"], "unchanged")
        self.assertEqual([c["signal"] for c in moved["changes"]], ["technical"])
        self.assertIn(f"now redirects to {www}en/ instead of {www}", moved["changes"][0]["summary"])


class RobotsTests(unittest.TestCase):
    class FakeParser:
        def parse(self, lines):
            self.lines = lines

        def can_fetch(self, agent, url):
            return "/private" not in url

    def checker(self, status=200, body="User-agent: *\nDisallow: /private", error=None):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return {"status": status, "final_url": url, "html": body, "error": error}
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
        self.assertFalse(self.checker(status=401, body="")[0].allowed("https://acme.example/a"))

    def test_robots_server_error_or_timeout_skips_the_site(self):
        for status, error, words in ((503, None, "returned HTTP 503"), (None, "timed out", "could not be read (timed out)")):
            with self.subTest(status=status, error=error):
                robots, calls = self.checker(status=status, body="", error=error)
                with tempfile.TemporaryDirectory() as state:
                    page = watch.check_page("https://acme.example/pricing", [], state, "2026-11-23", watch.live_fetcher(robots))
                self.assertEqual(page["status"], "skipped")
                self.assertIn(f"robots.txt for acme.example {words}", page["notes"][0])
                self.assertEqual(calls, ["https://acme.example/robots.txt"])  # the page itself was not fetched


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
        routes = {"/old": (301, {"Location": "/new"}, b""),
                  "/new": (200, {"Content-Type": "text/html; charset=utf-8"}, b"<h1>New page</h1>")}
        with local_server(routes) as (base, server):
            moved = watch.fetch_url(base + "/old", timeout=5)
            missing = watch.fetch_url(base + "/nope", timeout=5)
        self.assertEqual((moved["status"], moved["final_url"]), (200, base + "/new"))
        self.assertEqual(missing["status"], 404)
        self.assertTrue(all(a == watch.USER_AGENT for a in server.seen_agents))

    def test_connection_failure_returns_error_not_exception(self):
        result = watch.fetch_url("http://127.0.0.1:9/", timeout=2)
        self.assertIsNone(result["status"])
        self.assertTrue(result["error"])

    def test_meta_charset_is_used_when_the_header_has_none(self):
        body = ('<html><head><meta charset="windows-1252"><title>Café plans</title></head><body>'
                '<p>Our Café plan costs €9/mo and includes everything you need.</p></body></html>').encode("cp1252")
        equiv = body.replace(b'<meta charset="windows-1252">',
                             b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">')
        routes = {"/meta": (200, {"Content-Type": "text/html"}, body),
                  "/equiv": (200, {"Content-Type": "text/html"}, equiv)}
        with local_server(routes) as (base, _):
            pages = [watch.fetch_url(base + path, timeout=5) for path in routes]
        for page in pages:
            fields = watch.extract_fields(page["html"])
            self.assertEqual(fields["title"], "Café plans")
            self.assertIn("€9/mo", fields["prices"])

    def test_unknown_or_alias_charset_never_skips_the_page(self):
        text = "<h1>Café €9</h1>".encode("utf-8")
        routes = {"/alias": (200, {"Content-Type": "text/html; charset=utf8mb4"}, text),
                  "/unknown": (200, {"Content-Type": "text/html; charset=x-made-up"}, text),
                  "/meta-unknown": (200, {"Content-Type": "text/html"}, b'<meta charset="nonsense">' + text)}
        with local_server(routes) as (base, _), tempfile.TemporaryDirectory() as state:
            pages = [watch.check_page(base + path, [], state, "2026-11-23", watch.fetch_url) for path in routes]
            fetched = [watch.fetch_url(base + path, timeout=5) for path in routes]
        for page, result in zip(pages, fetched):
            with self.subTest(url=page["url"]):
                self.assertEqual(page["status"], "baseline")
                self.assertEqual(watch.extract_fields(result["html"])["h1"], ["Café €9"])
        self.assertEqual(watch.decode_html(b"caf\xe9"), "caf�")  # not UTF-8 and no charset: replaced, not skipped


class PacingTests(unittest.TestCase):
    def test_waits_between_every_request_including_robots(self):
        log = []

        def network(url):
            log.append(url)
            return {"status": 404 if url.endswith("robots.txt") else 200, "final_url": url, "html": PAGE, "error": None}

        def pause(seconds):
            log.append(f"wait {seconds}")

        fetch_paced = watch.paced(network, 2, sleep=pause)
        fetch = watch.live_fetcher(watch.RobotsChecker(fetch_paced), fetch_paced)
        config = {"competitors": [{"name": "A", "urls": ["https://a.example/", "https://a.example/b"]},
                                  {"name": "B", "urls": ["https://b.example/"]}]}
        with tempfile.TemporaryDirectory() as state:
            watch.run_watch(config, state, fetch, "2026-11-23")
        self.assertEqual(log, ["https://a.example/robots.txt", "wait 2", "https://a.example/", "wait 2",
                               "https://a.example/b", "wait 2", "https://b.example/robots.txt", "wait 2",
                               "https://b.example/"])


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
                         "HTTP 404, first seen 2026-11-23 on the first check of this page"):
            self.assertIn(expected, self.report)

    def test_problems_are_counted_separately(self):
        self.assertIn("Pages checked: 5. With changes: 3. Problems: 1. No change: 1.", self.report)
        self.assertIn("https://plankit.example/enterprise", self.report.split("## Problems")[1])
        self.assertNotIn("## Plankit", self.report)

    def test_noise_only_page_is_listed_under_no_change(self):
        no_change = self.report.split("## No change")[1]
        self.assertIn("https://plankit.example/", no_change)
        self.assertNotIn("Marcus", self.report)
        self.assertNotIn("November 16", self.report)

    def test_report_is_written_and_has_no_em_dashes(self):
        self.assertEqual(self.out.read_text(encoding="utf-8"), self.report)
        self.assertNotIn("—", self.report)

    def test_sample_report_in_the_repo_matches_the_demo(self):
        self.assertEqual(read("examples/sample-report.md"), self.report)

    def test_first_run_says_nothing_to_compare(self):
        config = watch.load_config(EXAMPLE_CONFIG)
        with tempfile.TemporaryDirectory() as state:
            results = watch.run_watch(config, state, watch.dir_fetcher(FIXTURES / "week1"), "2026-11-16")
        self.assertIn("first run", watch.render_report(results, "2026-11-16").splitlines()[0])

    def test_from_dir_cli_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.md"
            args = ["--config", str(EXAMPLE_CONFIG), "--state", tmp + "/state", "--out", str(out)]
            with quiet():
                watch.main(args + ["--from-dir", str(FIXTURES / "week1"), "--date", "2026-11-16"])
                watch.main(args + ["--from-dir", str(FIXTURES / "week2"), "--date", "2026-11-23"])
            self.assertTrue(out.read_text().startswith("Look at this first: Tallyboard homepage"))


class FirstRunTests(unittest.TestCase):
    def test_empty_from_dir_is_not_called_a_first_run_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "empty").mkdir()
            out = Path(tmp) / "report.md"
            args = ["--config", str(EXAMPLE_CONFIG), "--state", tmp + "/state", "--out", str(out),
                    "--from-dir", tmp + "/empty", "--date", "2026-11-16"]
            with quiet():
                code = watch.main(args)
            first = out.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(code, 1)
        self.assertNotIn("first run", first)
        self.assertEqual(first, "Look at this first: no page could be read this run. The reasons are below.")

    def test_new_page_on_a_later_run_is_not_called_a_first_run(self):
        config = {"competitors": [{"name": "Acme", "urls": ["https://acme.example/", "https://acme.example/new"]}]}

        def only_the_new_page(url):
            return page_fetch()(url) if url.endswith("/new") else {"skipped": "No saved file."}

        with tempfile.TemporaryDirectory() as state:
            check(state, "2026-11-16")
            results = watch.run_watch(config, state, only_the_new_page, "2026-11-23")
        first = watch.render_report(results, "2026-11-23").splitlines()[0]
        self.assertNotIn("first run", first)


class BadConfigTests(unittest.TestCase):
    def run_bad_config(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()) as err:
                watch.load_config(path)
        message = err.getvalue()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(message.count("\n"), 1, message)  # one clear line
        return message

    def run_bad(self, competitor, **top):
        return self.run_bad_config({"competitors": [competitor], **top})

    def test_url_without_scheme_is_rejected_cleanly(self):
        message = self.run_bad({"name": "Acme", "urls": ["notaurl"]})
        self.assertIn("Acme", message)
        self.assertIn("notaurl", message)

    def test_non_http_scheme_is_rejected(self):
        self.run_bad({"name": "Acme", "urls": ["ftp://acme.example/"]})

    def test_bad_regex_is_rejected_cleanly(self):
        message = self.run_bad({"name": "Acme", "urls": ["https://acme.example/"], "ignore": ["("]})
        self.assertIn("Bad regex in ignore for Acme: (", message)

    def test_ignore_as_a_string_is_rejected(self):
        message = self.run_bad({"name": "Acme", "urls": ["https://acme.example/"], "ignore": "Trusted by"})
        self.assertIn("'ignore' for Acme must be a list, like [\"Trusted by\"]", message)

    def test_urls_as_a_string_is_rejected(self):
        message = self.run_bad({"name": "Acme", "urls": "https://acme.example/"})
        self.assertIn("'urls' for Acme must be a list, like [\"https://acme.example/\"]", message)

    def test_keep_snapshots_must_be_a_positive_whole_number(self):
        for keep in ("8", 0, -1, True, 2.5):
            with self.subTest(keep=keep):
                message = self.run_bad({"name": "Acme", "urls": ["https://acme.example/"]}, keep_snapshots=keep)
                self.assertIn("keep_snapshots must be a whole number of 1 or more", message)

    def test_top_level_list_is_rejected(self):
        message = self.run_bad_config([{"name": "Acme", "urls": ["https://acme.example/"]}])
        self.assertIn("must be a JSON object", message)

    def test_bad_url_at_fetch_time_becomes_a_note(self):
        def boom(url):
            raise ValueError("unknown url type")
        with tempfile.TemporaryDirectory() as state:
            for url in ("notaurl", "https://acme.example/"):
                page = watch.check_page(url, [], state, "2026-11-23", boom)
                self.assertEqual(page["status"], "skipped")
                self.assertTrue(page["notes"])


class CliTests(unittest.TestCase):
    def test_date_must_be_yyyy_mm_dd(self):
        for bad in ("2026-13-01", "2026-11-31", "23-11-2026", "next monday"):
            with self.subTest(date=bad):
                with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()) as err:
                    watch.main(["--config", str(EXAMPLE_CONFIG), "--date", bad])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_out_folder_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "reports" / "2026" / "r.md"
            args = ["--config", str(EXAMPLE_CONFIG), "--state", tmp + "/state", "--out", str(out),
                    "--from-dir", str(FIXTURES / "week1"), "--date", "2026-11-16"]
            with quiet():
                code = watch.main(args)
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())

    def test_help_says_from_dir_reads_every_page(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()) as out:
            watch.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = " ".join(out.getvalue().split())
        self.assertIn("read every page in the watchlist from saved HTML in FOLDER", text)
        self.assertIn("Pages without a file are skipped.", text)


class WorkingFolderTests(unittest.TestCase):
    def test_script_uses_current_folder_not_repo_folder(self):
        fixtures = FIXTURES / "week1"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "pages").mkdir()
            for f in fixtures.glob("*.html"):
                (work / "pages" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            (work / "watchlist.json").write_text(EXAMPLE_CONFIG.read_text(), encoding="utf-8")
            cmd = [sys.executable, str(ROOT / "scripts" / "watch.py"), "--config", "watchlist.json", "--state", "state",
                   "--out", "report.md", "--from-dir", "pages", "--date", "2026-11-16"]
            done = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((work / "report.md").exists())
            self.assertTrue((work / "state").is_dir())


def code_block_after(text, marker):
    """The contents of the first ```text block after `marker`."""
    start = text.index("```text\n", text.index(marker)) + len("```text\n")
    return text[start:text.index("```", start)]


class RepoDocsTests(unittest.TestCase):
    def test_script_is_executable_and_has_no_lambda_assignments(self):
        self.assertTrue(os.access(ROOT / "scripts" / "watch.py", os.X_OK))
        for name in ("scripts/watch.py", "tests/test_watch.py"):
            tree = ast.parse(read(name))
            assigned = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Lambda)]
            self.assertEqual(assigned, [], name)

    def test_gitignore_covers_user_data(self):
        lines = set(read(".gitignore").splitlines())
        self.assertLessEqual({"watchlist.json", "state/", "report.md"}, lines)

    def test_skill_treats_page_text_as_data_and_does_not_tie_prices_to_plans(self):
        skill = read("SKILL.md")
        self.assertIn("third-party content", skill)
        self.assertIn("never as instructions", skill)
        self.assertIn("Do not assume which plan a price belongs to unless the page text", skill)
        self.assertNotIn("moved Pro", skill)

    def test_readme_excerpts_match_the_demo_files(self):
        readme = read("README.md")
        self.assertTrue(read("examples/sample-report.md").startswith(code_block_after(readme, "The top of it:")))
        self.assertTrue(read("examples/sample-brief.md").startswith(code_block_after(readme, "sample-brief.md`:")))

    def test_readme_test_count_is_current(self):
        count = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]).countTestCases()
        self.assertIn(f"{count} offline tests", read("README.md"))

    def test_readme_uses_a_data_folder_and_clear_wording(self):
        readme = read("README.md")
        for phrase in ("data folder outside the repo", "under 250 words", "fewer than 250 characters of visible text",
                       "reads every page in the watchlist", "except prices and plan names"):
            self.assertIn(phrase, readme)
        for stale in ("In the repo folder, copy", "Pro plan going from", "Prices are now taken out of the body text",
                      "waits two seconds between requests"):
            self.assertNotIn(stale, readme)

    def test_template_says_what_ignore_covers(self):
        notes = " ".join(json.loads(read("config/watchlist.template.json"))["_notes"])
        self.assertIn("Prices and plan names are always compared", notes)
        self.assertIn("page title, meta and social descriptions, headings, buttons and body sentences", notes)

    def test_public_docs_have_no_em_dashes(self):
        for name in ("README.md", "SKILL.md", "examples/sample-brief.md"):
            self.assertNotIn("—", read(name), name)

    def test_sample_brief_follows_the_skill_rules(self):
        brief, report = read("examples/sample-brief.md"), read("examples/sample-report.md")
        self.assertLess(len(brief.split()), 250)
        self.assertEqual(brief.splitlines()[0], report.splitlines()[0])
        for quoted in re.findall(r'"([^"]+)"', brief):
            self.assertIn(quoted, report)  # changed text is quoted exactly
        self.assertIn("One reading:", brief)
        self.assertIn("HTTP 404", brief)
        self.assertNotIn("Plankit Homepage", brief)  # no commentary on a page without a flagged change


if __name__ == "__main__":
    unittest.main()
