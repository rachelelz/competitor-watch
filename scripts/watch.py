#!/usr/bin/env python3
"""competitor-watch: snapshot competitor pages each week and report positioning changes.

Python 3.9+, standard library only.
Usage: python3 scripts/watch.py --config watchlist.json [--state DIR] [--out report.md] [--demo]
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.robotparser import RobotFileParser

USER_AGENT = "competitor-watch/0.1 (+https://github.com/rachelelz/competitor-watch)"
TIMEOUT_SECONDS = 15
DELAY_SECONDS = 2
KEEP_SNAPSHOTS = 8
MIN_VISIBLE_CHARS = 250  # below this a page is "thin", often built with JavaScript
MAX_BYTES = 2_000_000
REPO_ROOT = Path(__file__).resolve().parent.parent

# Elements and class names whose text is noise for positioning work.
SKIP_TAGS = {"script", "style", "nav", "footer", "noscript", "template", "svg", "iframe", "blockquote"}
SKIP_CLASS_PREFIXES = ("cookie", "consent", "gdpr", "testimonial", "review")
CTA_CLASS_PREFIXES = ("btn", "button", "cta")
PLAN_CLASS_TOKENS = {"plan", "tier"}
VOID_TAGS = {"meta", "link", "br", "img", "input", "hr", "source", "area", "base", "col", "embed", "wbr"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "section", "article", "header", "main", "aside", "tr", "td",
              "th", "table", "form", "h1", "h2", "h3", "h4", "h5", "h6", "button", "title", "body"}

# How much each kind of change matters when choosing "Look at this first".
SCORES = {"h1": 100, "price": 95, "plans": 90, "fetch_error": 88, "http_status": 88, "redirect": 85,
          "cta": 80, "meta": 75, "title": 70, "og": 65, "h2h3": 60, "copy": 20}


# ---------------------------------------------------------------- extraction

def clean(text):
    """Collapse all whitespace runs into single spaces."""
    return re.sub(r"\s+", " ", text or "").strip()


def unique(items):
    """Drop empty and repeated items, keeping order."""
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def class_tokens(attrs):
    """Words found in an element's class and id, lowercased."""
    raw = (attrs.get("class") or "") + " " + (attrs.get("id") or "")
    return [t for t in re.split(r"[\s_-]+", raw.lower()) if t]


class PageParser(HTMLParser):
    """Collects the fields we compare: head metadata, headings, CTAs, plans and visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title = ""
        self.meta = {}
        self.h1, self.h2h3, self.ctas, self.plans = [], [], [], []
        self.body_blocks, self.all_blocks = [], []
        self._body, self._all = [], []
        self._capture = None
        self._in_title = False

    def _skipping(self):
        return any(entry["skip"] for entry in self.stack)

    def _in_plan(self):
        return any(entry["plan"] for entry in self.stack)

    def flush(self):
        """End the current text block."""
        for parts, blocks in ((self._body, self.body_blocks), (self._all, self.all_blocks)):
            text = clean(" ".join(parts))
            if text:
                blocks.append(text)
            parts.clear()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            if key:
                self.meta[key] = clean(attrs.get("content"))
            return
        if tag in VOID_TAGS:
            if tag == "br":
                self.flush()
            return
        tokens = class_tokens(attrs)
        self.stack.append({
            "tag": tag,
            "depth": len(self.stack),
            "skip": tag in SKIP_TAGS or any(t.startswith(SKIP_CLASS_PREFIXES) for t in tokens),
            "plan": bool(PLAN_CLASS_TOKENS & set(tokens)),
        })
        self._in_title = self._in_title or tag == "title"
        if tag in BLOCK_TAGS:
            self.flush()
        if self._skipping() or self._capture:
            return
        is_cta = tag == "button" or (tag == "a" and any(t.startswith(CTA_CLASS_PREFIXES) for t in tokens))
        if tag in ("h1", "h2", "h3") or (tag == "h4" and self._in_plan()):
            self._capture = {"kind": tag, "depth": len(self.stack), "parts": []}
        elif is_cta:
            self._capture = {"kind": "cta", "depth": len(self.stack), "parts": []}

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        positions = [i for i, entry in enumerate(self.stack) if entry["tag"] == tag]
        if not positions:
            return
        in_plan = self._in_plan()
        del self.stack[positions[-1]:]
        if tag == "title":
            self._in_title = False
        if self._capture and len(self.stack) < self._capture["depth"]:
            self._finish_capture(clean(" ".join(self._capture["parts"])), in_plan)
            self._capture = None
        if tag in BLOCK_TAGS:
            self.flush()

    def _finish_capture(self, text, in_plan):
        kind = self._capture["kind"]
        if not text:
            return
        if kind == "cta":
            self.ctas.append(text)
        elif in_plan:
            self.plans.append(text)
        elif kind == "h1":
            self.h1.append(text)
        else:
            self.h2h3.append(text)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skipping():
            self._all.append(data)
            if self._capture:
                self._capture["parts"].append(data)
            else:
                self._body.append(data)


PRICE_UNIT = r"(?:\s*/\s*|\s+per\s+)"
PRICE_RE = re.compile(
    r"[$€£]\s?\d[\d,]*(?:\.\d{1,2})?"
    rf"(?:{PRICE_UNIT}(?:user|seat|member|mo|month|yr|year)\w*"
    rf"(?:{PRICE_UNIT}(?:mo|month|yr|year)\w*)?)?",
    re.IGNORECASE,
)


def find_prices(text):
    """Price-like strings: a currency symbol and a number, plus a unit such as '/mo' or 'per user'."""
    found = [re.sub(r"\s*/\s*", "/", clean(m.group(0))) for m in PRICE_RE.finditer(text)]
    return unique(found)


# Words that make a sentence "only a date". Anything left after removing them must be real text.
DATE_WORDS = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|updated|last|copyright|all|rights|reserved|posted|published|on|as|of|since|"
    r"year|st|nd|rd|th|per|user|seat|member|month|mo|yr)\b|[\d\W_]+",
    re.IGNORECASE,
)


def is_noise(sentence, ignore=()):
    """True for sentences that are only dates, numbers or a bare price, or that match a user ignore regex."""
    if len(DATE_WORDS.sub("", PRICE_RE.sub("", sentence))) < 3:
        return True
    return any(re.search(pattern, sentence, re.IGNORECASE) for pattern in ignore)


def split_sentences(blocks):
    """Split text blocks into sentences. Drops fragments shorter than three words."""
    sentences = []
    for block in blocks:
        for piece in re.split(r"(?<=[.!?])\s+", block):
            piece = piece.strip()
            if len(piece.split()) >= 3:
                sentences.append(piece)
    return unique(sentences)


def extract_fields(html):
    """Parse HTML into the comparable fields stored in a snapshot."""
    parser = PageParser()
    parser.feed(html)
    parser.close()
    parser.flush()
    meta = parser.meta
    return {
        "title": clean(parser.title),
        "meta_description": meta.get("description", ""),
        "og_title": meta.get("og:title", ""),
        "og_description": meta.get("og:description", ""),
        "h1": unique(parser.h1),
        "h2h3": unique(parser.h2h3),
        "ctas": unique(parser.ctas),
        "plans": unique(parser.plans),
        "prices": find_prices(" ".join(parser.all_blocks)),
        "sentences": split_sentences(parser.body_blocks)[:500],
        "visible_chars": sum(len(b) for b in parser.all_blocks),
    }


# ------------------------------------------------------------------ fetching

def fetch_url(url, timeout=TIMEOUT_SECONDS):
    """GET a URL with our User-Agent. Follows redirects and returns the final URL."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _read_response(response, response.status)
    except urllib.error.HTTPError as err:
        with err:
            return _read_response(err, err.code)
    except (urllib.error.URLError, OSError, ValueError) as err:
        return {"status": None, "final_url": url, "html": "", "error": str(getattr(err, "reason", err))}


def _read_response(response, status):
    charset = response.headers.get_content_charset() or "utf-8"
    html = response.read(MAX_BYTES).decode(charset, errors="replace")
    return {"status": status, "final_url": response.geturl(), "html": html, "error": None}


class RobotsChecker:
    """Checks robots.txt once per site. The fetcher and parser can be swapped out for tests."""

    def __init__(self, fetch=fetch_url, parser_factory=RobotFileParser):
        self.fetch, self.parser_factory, self._cache = fetch, parser_factory, {}

    def allowed(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._load(origin)
        rules = self._cache[origin]
        if rules == "deny":
            return False
        return rules is None or rules.can_fetch(USER_AGENT, url)

    def _load(self, origin):
        result = self.fetch(origin + "/robots.txt")
        status = result["status"]
        if status in (401, 403):
            return "deny"
        if status is None or status >= 400:
            return None  # no robots.txt: nothing is disallowed
        parser = self.parser_factory()
        parser.parse(result["html"].splitlines())
        return parser


def slug_for_url(url):
    """File-safe name for a URL: tallyboard.example/pricing becomes tallyboard.example-pricing."""
    parts = urllib.parse.urlsplit(url)
    raw = (parts.netloc + parts.path + ("?" + parts.query if parts.query else "")).lower()
    return re.sub(r"[^a-z0-9.]+", "-", raw).strip("-")


def live_fetcher(robots):
    """Fetch over the network, skipping URLs that robots.txt disallows."""
    def fetch(url):
        if not robots.allowed(url):
            return {"skipped": "robots.txt disallows this URL, so it was not fetched"}
        return fetch_url(url)
    return fetch


def dir_fetcher(directory):
    """Read saved HTML from DIR/<slug>.html. An optional <slug>.meta.json sets status and final_url."""
    directory = Path(directory)

    def fetch(url):
        slug = slug_for_url(url)
        html_path = directory / f"{slug}.html"
        if not html_path.exists():
            return {"skipped": f"no saved file named {slug}.html in {directory}"}
        meta_path = directory / f"{slug}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return {"status": meta.get("status", 200), "final_url": meta.get("final_url", url),
                "html": html_path.read_text(encoding="utf-8"), "error": None}
    return fetch


# ----------------------------------------------------------------- snapshots

def build_snapshot(url, fetched, today):
    """Turn a fetch result into the JSON we store."""
    ok = not fetched.get("error") and (fetched["status"] or 0) < 400
    fields = extract_fields(fetched["html"]) if ok else {}
    return {"version": 1, "url": url, "date": today, "status": fetched["status"],
            "final_url": fetched["final_url"], "error": fetched.get("error"), "fields": fields,
            "thin": bool(ok and fields["visible_chars"] < MIN_VISIBLE_CHARS)}


def load_previous(state_dir, slug, today):
    """Latest snapshot dated before today, or None."""
    older = sorted(p for p in (Path(state_dir) / slug).glob("*.json") if p.stem < today)
    return json.loads(older[-1].read_text(encoding="utf-8")) if older else None


def save_snapshot(state_dir, slug, snapshot, keep=KEEP_SNAPSHOTS):
    """Write today's snapshot and delete the oldest beyond `keep`."""
    folder = Path(state_dir) / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{snapshot['date']}.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    for old in sorted(folder.glob("*.json"))[:-keep]:
        old.unlink()


# ---------------------------------------------------------------------- diff

def norm(text):
    return clean(text).casefold()


def short(text, limit=140):
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def quote(text):
    return f'"{short(text)}"'


def make_change(signal, kind, label, summary, details):
    return {"signal": signal, "score": SCORES[kind], "title": f"{label} changed", "summary": summary, "details": details}


def scalar_change(old, new, label, signal, kind):
    """Change for a single text field such as the title or meta description."""
    if norm(old) == norm(new):
        return None
    if old and new:
        summary = f"{label} changed from {quote(old)} to {quote(new)}"
        details = [f'Was: "{old}"', f'Now: "{new}"']
    elif new:
        summary, details = f"{label} added: {quote(new)}", [f'Now: "{new}"']
    else:
        summary, details = f"{label} removed: {quote(old)}", [f'Was: "{old}"']
    return make_change(signal, kind, label, summary, details)


def list_change(old, new, label, signal, kind):
    """Change for a list field such as headings, CTAs or prices."""
    old_keys, new_keys = {norm(x) for x in old}, {norm(x) for x in new}
    removed = [x for x in old if norm(x) not in new_keys]
    added = [x for x in new if norm(x) not in old_keys]
    if not removed and not added:
        return None
    if len(removed) == 1 and len(added) == 1:
        return make_change(signal, kind, label, f"{label} changed from {quote(removed[0])} to {quote(added[0])}",
                           [f'Was: "{removed[0]}"', f'Now: "{added[0]}"'])
    parts = []
    if added:
        parts.append("added " + ", ".join(quote(x) for x in added[:3]))
    if removed:
        parts.append("removed " + ", ".join(quote(x) for x in removed[:3]))
    details = [f'Added: "{x}"' for x in added] + [f'Removed: "{x}"' for x in removed]
    return make_change(signal, kind, label, f"{label} changed: " + "; ".join(parts), details)


def url_key(url):
    """Compare URLs without caring about trailing slashes, fragments or host case."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


def technical_changes(old, new):
    """Status code, redirect and fetch-failure changes. `old` is None for a page seen the first time."""
    changes, first = [], old is None
    prefix = "new in watchlist, " if first else ""

    def add(kind, summary):
        changes.append(make_change("technical", kind, "Page status", summary, [summary[0].upper() + summary[1:] + "."]))

    if new.get("error"):
        if first or not old.get("error"):
            add("fetch_error", f"{prefix}the page could not be fetched ({new['error']})")
        return changes
    status, old_status = new["status"], (old or {}).get("status") or 200
    if status >= 400 and (first or old_status < 400 or old_status != status):
        add("http_status", f"{prefix}the page returns HTTP {status}")
    elif status < 400 and not first and old_status >= 400:
        add("http_status", f"the page is reachable again (HTTP {status})")
    redirect = new["final_url"] if url_key(new["final_url"]) != url_key(new["url"]) else ""
    old_redirect = ""
    if old and url_key(old["final_url"]) != url_key(old["url"]):
        old_redirect = old["final_url"]
    if status < 400 and url_key(redirect) != url_key(old_redirect):
        if redirect:
            add("redirect", f"{prefix}the page now redirects to {redirect}")
        else:
            add("redirect", "the page no longer redirects")
    return changes


THIN_NOTE = ("Very little visible text. The page may be built with JavaScript, which this tool cannot run. "
             "Text changes are not reported for this page. Save the rendered HTML by hand and use --from-dir if you need it.")


def diff_snapshots(old, new, ignore=()):
    """Compare two snapshots. Returns (changes sorted by importance, notes)."""
    changes, notes = technical_changes(old, new), []
    if new.get("error") or new["status"] >= 400 or old is None:
        return changes, notes
    if old.get("error") or old["status"] >= 400:
        return changes, ["The previous snapshot had no page content, so text was not compared."]
    if new["thin"] or old["thin"]:
        return changes, [THIN_NOTE]
    o, n = old["fields"], new["fields"]
    candidates = [
        list_change(o["h1"], n["h1"], "headline (H1)", "positioning", "h1"),
        scalar_change(o["title"], n["title"], "page title", "positioning", "title"),
        scalar_change(o["meta_description"], n["meta_description"], "meta description", "positioning", "meta"),
        scalar_change(o["og_title"], n["og_title"], "social (og) title", "positioning", "og"),
        scalar_change(o["og_description"], n["og_description"], "social (og) description", "positioning", "og"),
        list_change(*_filtered(o["h2h3"], n["h2h3"], ignore), "section headings (H2/H3)", "positioning", "h2h3"),
        list_change(*_filtered(o["ctas"], n["ctas"], ignore), "button and link text (CTA)", "positioning", "cta"),
        list_change(o["prices"], n["prices"], "prices", "pricing", "price"),
        list_change(o["plans"], n["plans"], "plan names", "pricing", "plans"),
        copy_change(o["sentences"], n["sentences"], ignore),
    ]
    changes += [c for c in candidates if c]
    return sorted(changes, key=lambda c: -c["score"]), notes


def _filtered(old, new, ignore):
    """Remove items matching an ignore regex from both lists before comparing."""
    keep = lambda items: [x for x in items if not any(re.search(p, x, re.IGNORECASE) for p in ignore)]
    return keep(old), keep(new)


def copy_change(old, new, ignore, max_shown=6):
    """Added and removed body sentences, after the noise filter."""
    old_keys, new_keys = {norm(s) for s in old}, {norm(s) for s in new}
    added = [s for s in new if norm(s) not in old_keys and not is_noise(s, ignore)]
    removed = [s for s in old if norm(s) not in new_keys and not is_noise(s, ignore)]
    if not added and not removed:
        return None
    details = []
    for tag, items in (("Added", added), ("Removed", removed)):
        details += [f'{tag}: "{short(s)}"' for s in items[:max_shown]]
        if len(items) > max_shown:
            details.append(f"{tag}: and {len(items) - max_shown} more sentences")
    summary = f"body copy changed ({len(added)} sentences added, {len(removed)} removed)"
    change = make_change("copy", "copy", "body copy", summary, details)
    change["score"] += min(len(added) + len(removed), 10)
    return change


# ------------------------------------------------------------------- running

def page_place(url):
    path = urllib.parse.urlsplit(url).path
    return "Homepage" if path in ("", "/") else path


def check_page(url, ignore, state_dir, today, fetch, keep=KEEP_SNAPSHOTS):
    """Fetch one page, save its snapshot and compare it with the previous one."""
    result = {"url": url, "place": page_place(url), "status": "unchanged", "changes": [], "notes": [], "previous": None}
    fetched = fetch(url)
    if fetched.get("skipped"):
        result.update(status="skipped", notes=[fetched["skipped"]])
        return result
    slug = slug_for_url(url)
    snapshot = build_snapshot(url, fetched, today)
    previous = load_previous(state_dir, slug, today)
    save_snapshot(state_dir, slug, snapshot, keep)
    result["changes"], result["notes"] = diff_snapshots(previous, snapshot, ignore)
    result["previous"] = previous["date"] if previous else None
    if previous is None:
        result["status"] = "baseline"
    elif result["changes"]:
        result["status"] = "changed"
    return result


def run_watch(config, state_dir, fetch, today, delay=0, sleep=time.sleep):
    """Check every page in the config. Returns one entry per competitor."""
    keep, results, first = config.get("keep_snapshots", KEEP_SNAPSHOTS), [], True
    for competitor in config["competitors"]:
        pages = []
        for url in competitor["urls"]:
            if delay and not first:
                sleep(delay)
            first = False
            pages.append(check_page(url, competitor.get("ignore", []), state_dir, today, fetch, keep))
        results.append({"name": competitor["name"], "pages": pages})
    return results


def load_config(path):
    """Read and check the watchlist file."""
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        sys.exit(f"Could not read {path}: {err}")
    if not config.get("competitors"):
        sys.exit("The watchlist needs a 'competitors' list.")
    for competitor in config["competitors"]:
        if not competitor.get("name") or not competitor.get("urls"):
            sys.exit("Each competitor needs a 'name' and at least one URL in 'urls'.")
        for pattern in competitor.get("ignore", []):
            re.compile(pattern)
    return config


# -------------------------------------------------------------------- report

SIGNAL_NAMES = {"positioning": "Positioning", "pricing": "Pricing", "copy": "Copy", "technical": "Technical"}


def top_change(results):
    """The single highest-scoring change as (competitor, page, change), or None."""
    best = None
    for competitor in results:
        for page in competitor["pages"]:
            for change in page["changes"]:
                if best is None or change["score"] > best[2]["score"]:
                    best = (competitor["name"], page, change)
    return best


def look_first_line(results):
    best = top_change(results)
    if best:
        name, page, change = best
        place = "homepage" if page["place"] == "Homepage" else f"{page['place']} page"
        return f"Look at this first: {name} {place}, {change['summary']}."
    pages = [p for c in results for p in c["pages"]]
    if pages and all(p["status"] in ("baseline", "skipped") for p in pages):
        return ("Look at this first: nothing yet. This was the first run, so snapshots are saved "
                "and next week's report will show what changed.")
    return "Look at this first: nothing changed. Details are below."


def render_page(page):
    when = "first snapshot" if page["previous"] is None else f"compared with {page['previous']}"
    lines = [f"### {page['place']}", f"{page['url']} ({when})", ""]
    for change in page["changes"]:
        lines.append(f"- **{SIGNAL_NAMES[change['signal']]}: {change['title']}**")
        lines += [f"  - {detail}" for detail in change["details"]]
    return lines + [""]


def render_report(results, today):
    """Build the markdown report."""
    pages = [(c["name"], p) for c in results for p in c["pages"]]
    count = lambda status: sum(1 for _, p in pages if p["status"] == status)
    with_changes = sum(1 for _, p in pages if p["changes"])
    stats = f"Pages checked: {len(pages) - count('skipped')}. With changes: {with_changes}. No change: {count('unchanged')}."
    if any(p["status"] == "baseline" and not p["changes"] for _, p in pages):
        stats += f" First snapshot, nothing to compare yet: {sum(1 for _, p in pages if p['status'] == 'baseline' and not p['changes'])}."
    if count("skipped"):
        stats += f" Skipped: {count('skipped')}."
    lines = [look_first_line(results), "", f"# Competitor watch: {today}", "", stats, ""]
    flagged = sorted((c for c in results if any(p["changes"] for p in c["pages"])),
                     key=lambda c: -max(ch["score"] for p in c["pages"] for ch in p["changes"]))
    for competitor in flagged:
        lines += [f"## {competitor['name']}", ""]
        for page in competitor["pages"]:
            if page["changes"]:
                lines += render_page(page)
    quiet = [(n, p) for n, p in pages if p["status"] == "unchanged"]
    if quiet:
        lines += ["## No change", ""] + [f"- {n} {p['place']}: {p['url']}" for n, p in quiet] + [""]
    first = [(n, p) for n, p in pages if p["status"] == "baseline" and not p["changes"]]
    if first:
        lines += ["## First snapshot saved", ""] + [f"- {n} {p['place']}: {p['url']}" for n, p in first] + [""]
    notes = [(n, p, note) for n, p in pages for note in p["notes"]]
    if notes:
        lines += ["## Notes", ""] + [f"- {n} {p['place']}: {note}" for n, p, note in notes] + [""]
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------- CLI

def run_demo(out_path=None, state_dir=None):
    """Run two fake weeks offline from examples/fixtures and write the sample report."""
    config = load_config(REPO_ROOT / "examples" / "watchlist.example.json")
    out_path = Path(out_path or REPO_ROOT / "examples" / "sample-report.md")
    with tempfile.TemporaryDirectory() as scratch:
        state = state_dir or scratch
        for week, day in (("week1", "2026-11-16"), ("week2", "2026-11-23")):
            fetch = dir_fetcher(REPO_ROOT / "examples" / "fixtures" / week)
            results = run_watch(config, state, fetch, day)
    report = render_report(results, "2026-11-23")
    out_path.write_text(report, encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Snapshot competitor pages and report positioning changes.")
    ap.add_argument("--config", help="watchlist JSON file")
    ap.add_argument("--state", default="state", help="folder for snapshots (default: ./state)")
    ap.add_argument("--out", help="report path (default: report.md, or examples/sample-report.md with --demo)")
    ap.add_argument("--from-dir", help="read saved HTML from this folder instead of fetching")
    ap.add_argument("--demo", action="store_true", help="run offline on the example fixtures")
    ap.add_argument("--date", help="snapshot date as YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)

    if args.demo:
        run_demo(args.out)
        print(f"Wrote {args.out or REPO_ROOT / 'examples' / 'sample-report.md'}")
        return 0
    if not args.config:
        ap.error("--config is required unless you use --demo")
    config = load_config(args.config)
    today = args.date or datetime.date.today().isoformat()
    if args.from_dir:
        fetch, delay = dir_fetcher(args.from_dir), 0
    else:
        fetch, delay = live_fetcher(RobotsChecker()), DELAY_SECONDS
    results = run_watch(config, args.state, fetch, today, delay)
    out = Path(args.out or "report.md")
    out.write_text(render_report(results, today), encoding="utf-8")
    print(f"Wrote {out}. Snapshots are in {args.state}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
