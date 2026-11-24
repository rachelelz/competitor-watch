#!/usr/bin/env python3
"""competitor-watch: snapshot competitor pages each week and report positioning changes.

Python 3.9+, standard library only.
Usage: python3 scripts/watch.py --config watchlist.json [--state DIR] [--out report.md] [--from-dir DIR] [--demo]
"""
from __future__ import annotations

import argparse
import datetime
import http.client
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
MIN_VISIBLE_CHARS = 250  # a page with fewer characters of visible text than this is "thin", often built with JavaScript
MAX_BYTES = 2_000_000
REPO_ROOT = Path(__file__).resolve().parent.parent

# Elements and class names whose text is noise for positioning work.
SKIP_TAGS = {"script", "style", "nav", "footer", "noscript", "template", "svg", "iframe", "blockquote"}
SKIP_CLASS_PREFIXES = ("cookie", "consent", "gdpr", "testimonial", "review")
CTA_CLASS_PREFIXES = ("btn", "button", "cta")
# Buttons with these class words are carousel arrows or accordion toggles, not calls to action.
NOT_CTA_CLASS_TOKENS = {"carousel", "slider", "swiper", "slick", "splide", "glide", "owl", "flickity", "accordion"}
NOT_CTA_TEXT = re.compile(r"(?:next|prev|previous)(?: slide| image)?|[<>‹›«»←→]", re.IGNORECASE)
PLAN_CLASS_TOKENS = {"plan", "plans", "tier"}
PLAN_CLASS_NAMES = {"pricing-card", "pricing-tier", "price-card"}
VOID_TAGS = {"meta", "link", "br", "img", "input", "hr", "source", "area", "base", "col", "embed", "wbr"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "section", "article", "header", "main", "aside", "tr", "td",
              "th", "table", "form", "h1", "h2", "h3", "h4", "h5", "h6", "button", "title", "body"}
# A new <p> or <li> closes the open one, as browsers do, unless one of these elements sits in between.
IMPLICIT_CLOSE_BARRIERS = {
    "p": {"div", "section", "article", "header", "main", "aside", "form", "ul", "ol", "li", "table", "td", "th",
          "button", "blockquote"},
    "li": {"ul", "ol"},
}

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


def plural(count, word):
    """'1 sentence', '2 sentences'."""
    return f"{count} {word}" + ("" if count == 1 else "s")


def class_tokens(attrs):
    """Words found in an element's class and id, lowercased."""
    raw = (attrs.get("class") or "") + " " + (attrs.get("id") or "")
    return [t for t in re.split(r"[\s_-]+", raw.lower()) if t]


def class_names(attrs):
    """Whole class and id names, lowercased, with underscores read as hyphens."""
    raw = (attrs.get("class") or "") + " " + (attrs.get("id") or "")
    return set(raw.lower().replace("_", "-").split())


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
        self._title_done = False

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

    def _close_open(self, tag):
        """Close an open <p> or <li> before a new one starts, so unclosed tags don't pile up."""
        barriers = IMPLICIT_CLOSE_BARRIERS[tag]
        for entry in reversed(self.stack):
            if entry["tag"] == tag:
                self.handle_endtag(tag)
                return
            if entry["tag"] in barriers:
                return

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
        if tag in IMPLICIT_CLOSE_BARRIERS:
            self._close_open(tag)
        tokens = class_tokens(attrs)
        # Only the document <title> counts, not a <title> inside an inline <svg> icon.
        if tag == "title" and not self._title_done and not self._skipping():
            self._in_title = True
        self.stack.append({
            "tag": tag,
            "depth": len(self.stack),
            "skip": tag in SKIP_TAGS or any(t.startswith(SKIP_CLASS_PREFIXES) for t in tokens),
            "plan": bool(PLAN_CLASS_TOKENS & set(tokens) or PLAN_CLASS_NAMES & class_names(attrs)),
        })
        if tag in BLOCK_TAGS:
            self.flush()
        if self._skipping() or self._capture:
            return
        is_cta = tag == "button" or (tag == "a" and any(t.startswith(CTA_CLASS_PREFIXES) for t in tokens))
        if is_cta and ("aria-expanded" in attrs or NOT_CTA_CLASS_TOKENS & set(tokens)):
            is_cta = False
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
        if self._in_title and not any(entry["tag"] == "title" for entry in self.stack):
            self._in_title, self._title_done = False, True
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
            if not NOT_CTA_TEXT.fullmatch(text):
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


PRICE_NUMBER = r"\d(?:[\d,.]*\d)?"
PRICE_UNIT = r"(?:\s*/\s*|\s+per\s+)"
PRICE_RE = re.compile(
    # $12, € 12, 12 €, USD 49 or 49 EUR ...
    rf"(?:[$€£]\s?{PRICE_NUMBER}|{PRICE_NUMBER}\s?[$€£](?!\s?\d)"
    rf"|(?-i:\b(?:USD|EUR|GBP)\s?){PRICE_NUMBER}|{PRICE_NUMBER}\s?(?-i:(?:USD|EUR|GBP)\b))"
    # ... plus an optional unit such as "/mo", "per user / month" or "per seat per year".
    rf"(?:{PRICE_UNIT}(?:user|seat|member|mo|month|yr|year)\w*"
    rf"(?:{PRICE_UNIT}(?:mo|month|yr|year)\w*)?)?",
    re.IGNORECASE,
)


def find_prices(text):
    """Price-like strings: a currency symbol or code and a number, plus a unit such as '/mo' or 'per user'."""
    found = [re.sub(r"\s*/\s*", "/", clean(m.group(0))) for m in PRICE_RE.finditer(text)]
    return unique(found)


def mask_prices(text):
    """Swap every price in a sentence for a placeholder, so a price change alone isn't a copy change."""
    return PRICE_RE.sub("<price>", text)


# Words that make a sentence "only a date". Anything left after removing them must be real text.
DATE_WORDS = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|updated|last|copyright|all|rights|reserved|posted|published|on|as|of|since|"
    r"year|st|nd|rd|th|per|user|seat|member|month|mo|yr)\b|[\d\W_]+",
    re.IGNORECASE,
)


def matches_ignore(text, ignore):
    """True if a user ignore regex matches the text."""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in ignore)


def is_noise(sentence, ignore=()):
    """True for sentences that are only dates, numbers or a bare price, or that match a user ignore regex."""
    if len(DATE_WORDS.sub("", PRICE_RE.sub("", sentence))) < 3:
        return True
    return matches_ignore(sentence, ignore)


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

META_CHARSET_RE = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([a-z0-9_.:-]+)", re.IGNORECASE)
CHARSET_ALIASES = {"utf8": "utf-8", "utf8mb4": "utf-8", "utf8mb3": "utf-8",
                   # Browsers read these labels as windows-1252, which is what such pages usually are.
                   "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252",
                   "us-ascii": "cp1252", "ascii": "cp1252"}


def usable_charset(label):
    """A Python text codec for a charset label, or None if the label is missing or unknown."""
    if not label:
        return None
    name = label.strip().strip("\"'").lower()
    name = CHARSET_ALIASES.get(name, name)
    try:
        b"".decode(name)
    except LookupError:
        return None
    return name


def decode_html(raw, header_charset=None):
    """Decode page bytes with the HTTP header's charset, else a <meta> charset, else UTF-8. Bad bytes become U+FFFD."""
    match = META_CHARSET_RE.search(raw[:4096])
    meta_charset = match.group(1).decode("ascii", "replace") if match else None
    charset = usable_charset(header_charset) or usable_charset(meta_charset) or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return raw.decode("utf-8", errors="replace")


def fetch_url(url, timeout=TIMEOUT_SECONDS):
    """GET a URL with our User-Agent. Follows redirects and returns the final URL."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _read_response(response, response.status)
    except urllib.error.HTTPError as err:
        with err:
            return _read_response(err, err.code)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as err:
        return {"status": None, "final_url": url, "html": "", "error": str(getattr(err, "reason", err))}


def _read_response(response, status):
    html = decode_html(response.read(MAX_BYTES), response.headers.get_content_charset())
    return {"status": status, "final_url": response.geturl(), "html": html, "error": None}


def paced(fetch, delay=DELAY_SECONDS, sleep=time.sleep):
    """Wrap a fetcher so it waits `delay` seconds before every request except the first."""
    first = [True]

    def fetch_paced(url):
        if not first[0]:
            sleep(delay)
        first[0] = False
        return fetch(url)
    return fetch_paced


# Plain words for common network errors. Anything else is shown as the raw error.
ERROR_WORDS = (
    (("nodename nor servname", "name or service not known", "temporary failure in name resolution",
      "getaddrinfo failed", "no address associated"), "DNS lookup failed"),
    (("timed out",), "timed out"),
    (("connection refused",), "connection refused"),
    (("connection reset",), "connection reset"),
    (("certificate",), "certificate check failed"),
    (("ssl",), "secure connection failed"),
    (("remote end closed",), "the server closed the connection"),
)


def friendly_error(raw):
    """'[Errno 8] nodename nor servname provided, or not known' becomes 'DNS lookup failed'."""
    low = (raw or "").lower()
    for needles, words in ERROR_WORDS:
        if any(needle in low for needle in needles):
            return words
    return raw or "unknown error"


class RobotsChecker:
    """Checks robots.txt once per site per run. The fetcher and parser can be swapped out for tests."""

    def __init__(self, fetch=fetch_url, parser_factory=RobotFileParser):
        self.fetch, self.parser_factory, self._cache = fetch, parser_factory, {}

    def allowed(self, url):
        return self.skip_reason(url) is None

    def skip_reason(self, url):
        """None if the URL may be fetched, else a sentence saying why it is skipped."""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._load(origin, parts.netloc)
        rules = self._cache[origin]
        if isinstance(rules, str):
            return rules
        if rules is None or rules.can_fetch(USER_AGENT, url):
            return None
        return "robots.txt disallows this URL, so it was not fetched."

    def _load(self, origin, host):
        result = self.fetch(origin + "/robots.txt")
        status = result.get("status")
        if result.get("error"):
            return (f"robots.txt for {host} could not be read ({friendly_error(result['error'])}), "
                    "so this page was skipped this run.")
        if status in (401, 403):
            return f"robots.txt for {host} returned HTTP {status}, which counts as a ban, so this page was skipped."
        if status is not None and status >= 500:
            return f"robots.txt for {host} returned HTTP {status}, so this page was skipped this run."
        if status is not None and status >= 400:
            return None  # no robots.txt: nothing is disallowed
        parser = self.parser_factory()
        parser.parse(result["html"].splitlines())
        return parser


def slug_for_url(url):
    """File-safe name for a URL: tallyboard.example/pricing becomes tallyboard.example-pricing."""
    parts = urllib.parse.urlsplit(url)
    raw = (parts.netloc + parts.path + ("?" + parts.query if parts.query else "")).lower()
    return re.sub(r"[^a-z0-9.]+", "-", raw).strip("-")


def live_fetcher(robots, fetch=fetch_url):
    """Fetch over the network, skipping URLs that robots.txt disallows or that it can't vouch for."""
    def fetch_page(url):
        reason = robots.skip_reason(url)
        if reason:
            return {"skipped": reason}
        return fetch(url)
    return fetch_page


def dir_fetcher(directory):
    """Read every page from DIR/<slug>.html. An optional <slug>.meta.json sets status and final_url."""
    directory = Path(directory)

    def fetch(url):
        slug = slug_for_url(url)
        html_path = directory / f"{slug}.html"
        if not html_path.exists():
            return {"skipped": f"No saved file named {slug}.html in {directory}, so it was skipped. "
                               "--from-dir reads every page from that folder."}
        meta_path = directory / f"{slug}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return {"status": meta.get("status", 200), "final_url": meta.get("final_url", url),
                "html": decode_html(html_path.read_bytes()), "error": None}
    return fetch


# ----------------------------------------------------------------- snapshots

SNAPSHOT_NAME = re.compile(r"\d{4}-\d{2}-\d{2}")
# The fields extract_fields() stores, and their types. A readable snapshot must have all of them.
FIELD_TYPES = {"title": str, "meta_description": str, "og_title": str, "og_description": str, "h1": list,
               "h2h3": list, "ctas": list, "plans": list, "prices": list, "sentences": list, "visible_chars": int}


def snapshot_files(folder):
    """Dated snapshot files in one page's folder, oldest first."""
    return sorted(p for p in Path(folder).glob("*.json") if SNAPSHOT_NAME.fullmatch(p.stem))


def problem_key(snapshot):
    """What went wrong with a fetch ('unreachable' or 'HTTP 404'), or None if the page was read."""
    if snapshot.get("error"):
        return "unreachable"
    status = snapshot.get("status")
    return f"HTTP {status}" if status is not None and status >= 400 else None


def has_content(snapshot):
    """True for a snapshot of a page that was read, so there is text to compare with."""
    return problem_key(snapshot) is None and bool(snapshot.get("fields"))


def build_snapshot(url, fetched, today):
    """Turn a fetch result into the JSON we store."""
    ok = not fetched.get("error") and (fetched["status"] or 0) < 400
    fields = extract_fields(fetched["html"]) if ok else {}
    return {"version": 1, "url": url, "date": today, "status": fetched["status"],
            "final_url": fetched["final_url"], "error": fetched.get("error"), "fields": fields,
            "thin": bool(ok and fields["visible_chars"] < MIN_VISIBLE_CHARS), "problem_since": None}


def read_snapshot(path):
    """Load one snapshot file. Raises ValueError with a plain reason if it is empty, not JSON or the wrong shape."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as err:
        raise ValueError(f"the file could not be read ({err})") from err
    if not text.strip():
        raise ValueError("the file is empty")
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ValueError("the file is not valid JSON") from err
    status = data.get("status") if isinstance(data, dict) else None
    fields = data.get("fields") if isinstance(data, dict) else None
    if (not isinstance(fields, dict) or not isinstance(data.get("final_url"), str)
            or isinstance(status, bool) or not isinstance(status, (int, type(None)))
            or (fields and not all(isinstance(fields.get(k), kind) for k, kind in FIELD_TYPES.items()))):
        raise ValueError("the file does not look like a snapshot")
    data.setdefault("date", path.stem)
    data.setdefault("thin", False)
    return data


def load_history(state_dir, slug, today):
    """Earlier snapshots of one page: the latest, the latest with page content, and notes on damaged files."""
    files = [p for p in snapshot_files(Path(state_dir) / slug) if p.stem < today]
    history = {"seen": bool(files), "latest": None, "good": None, "notes": []}
    for path in reversed(files):
        try:
            snapshot = read_snapshot(path)
        except ValueError as err:
            history["notes"].append(f"Skipped the damaged snapshot file {slug}/{path.name} because {err}.")
            continue
        if history["latest"] is None:
            history["latest"] = snapshot
        if has_content(snapshot):
            history["good"] = snapshot
            break
    return history


def load_previous(state_dir, slug, today):
    """The latest snapshot dated before today that has page content, or None."""
    return load_history(state_dir, slug, today)["good"]


def _is_good_file(path):
    try:
        return has_content(read_snapshot(path))
    except ValueError:
        return False


def save_snapshot(state_dir, slug, snapshot, keep=KEEP_SNAPSHOTS):
    """Write today's snapshot and delete the oldest beyond `keep`. The newest snapshot with page content stays."""
    folder = Path(state_dir) / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{snapshot['date']}.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    files = snapshot_files(folder)
    extra = files[:-keep]
    if extra:
        newest_good = next((p for p in reversed(files) if _is_good_file(p)), None)
        for old in extra:
            if old != newest_good:
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


def technical_change(kind, summary):
    return make_change("technical", kind, "Page status", summary, [summary[0].upper() + summary[1:] + "."])


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


def drop_ignored(items, ignore):
    """Leave out list items that match an ignore regex."""
    return [x for x in items if not matches_ignore(x, ignore)]


def blank_if_ignored(text, ignore):
    """Treat a single text field that matches an ignore regex as empty."""
    return "" if text and matches_ignore(text, ignore) else text


def url_key(url):
    """Compare URLs without caring about trailing slashes, fragments or host case."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


THIN_NOTE = (f"Very little visible text (under {MIN_VISIBLE_CHARS} characters). The page may be built with "
             "JavaScript, which this tool can't run. Its title, meta description and social tags are still compared. "
             "Its headings, buttons, prices, plan names and body text are not. To compare those, save every page in "
             "the watchlist as HTML and run with --from-dir.")


def diff_snapshots(old, new, ignore=(), latest=None):
    """Compare today's readable snapshot `new` with `old`, the last earlier snapshot with page content (or None).
    `latest` is the most recent earlier snapshot, which may be a failed one. Returns (changes, notes)."""
    changes, notes = [], []
    if latest is not None and problem_key(latest):
        status = f" (HTTP {new['status']})" if new.get("status") else ""
        changes.append(technical_change("http_status", f"the page can be read again{status}"))
    redirects = url_key(new["final_url"]) != url_key(new["url"])
    if old is None:
        if redirects:
            notes.append(f"This page redirects to {new['final_url']}. That is noted on a first snapshot, not flagged. "
                         "Later runs flag it only if the final address changes.")
    elif url_key(new["final_url"]) != url_key(old["final_url"]):
        old_redirected = url_key(old["final_url"]) != url_key(new["url"])
        if redirects:
            extra = f" instead of {old['final_url']}" if old_redirected else ""
            changes.append(technical_change("redirect", f"the page now redirects to {new['final_url']}{extra}"))
        else:
            changes.append(technical_change("redirect", f"the page no longer redirects to {old['final_url']}"))
    if new["thin"]:
        notes.append(THIN_NOTE)
    if old is None:
        return sorted(changes, key=lambda c: -c["score"]), notes
    o, n = old["fields"], new["fields"]

    def head(field):
        return blank_if_ignored(o[field], ignore), blank_if_ignored(n[field], ignore)

    def body(field):
        return drop_ignored(o[field], ignore), drop_ignored(n[field], ignore)

    candidates = [
        scalar_change(*head("title"), "page title", "positioning", "title"),
        scalar_change(*head("meta_description"), "meta description", "positioning", "meta"),
        scalar_change(*head("og_title"), "social (og) title", "positioning", "og"),
        scalar_change(*head("og_description"), "social (og) description", "positioning", "og"),
    ]
    if old["thin"] and not new["thin"]:
        notes.append(f"The {old['date']} snapshot had very little visible text, so headings, buttons, prices, "
                     "plan names and body text were not compared this run.")
    if not (old["thin"] or new["thin"]):
        candidates += [
            list_change(*body("h1"), "headline (H1)", "positioning", "h1"),
            list_change(*body("h2h3"), "section headings (H2/H3)", "positioning", "h2h3"),
            list_change(*body("ctas"), "button and link text (CTA)", "positioning", "cta"),
            list_change(o["prices"], n["prices"], "prices", "pricing", "price"),
            list_change(o["plans"], n["plans"], "plan names", "pricing", "plans"),
            copy_change(o["sentences"], n["sentences"], ignore),
        ]
    changes += [c for c in candidates if c]
    return sorted(changes, key=lambda c: -c["score"]), notes


def copy_change(old, new, ignore, max_shown=6):
    """Added and removed body sentences, after the noise filter. Prices are masked, so a price-only edit is skipped."""
    def key(sentence):
        return norm(mask_prices(sentence))

    old_keys, new_keys = {key(s) for s in old}, {key(s) for s in new}
    added = [s for s in new if key(s) not in old_keys and not is_noise(s, ignore)]
    removed = [s for s in old if key(s) not in new_keys and not is_noise(s, ignore)]
    if not added and not removed:
        return None
    details = []
    for tag, items in (("Added", added), ("Removed", removed)):
        details += [f'{tag}: "{short(s)}"' for s in items[:max_shown]]
        hidden = len(items) - max_shown
        if hidden > 0:
            details.append(f"{hidden} more {tag.lower()} {'sentence' if hidden == 1 else 'sentences'} not shown")
    summary = f"body copy changed ({plural(len(added), 'sentence')} added, {len(removed)} removed)"
    change = make_change("copy", "copy", "body copy", summary, details)
    change["score"] += min(len(added) + len(removed), 10)
    return change


# ------------------------------------------------------------------ problems

def problem_text(snapshot):
    """'HTTP 404' or "couldn't reach acme.example (DNS lookup failed)"."""
    if snapshot.get("error"):
        host = urllib.parse.urlsplit(snapshot["url"]).hostname or snapshot["url"]
        return f"couldn't reach {host} ({friendly_error(snapshot['error'])})"
    return f"HTTP {snapshot['status']}"


def describe_problem(snapshot, latest, good):
    """A page that could not be read this run. `state` is first, new or ongoing."""
    if latest is None:
        state = "first"
    elif problem_key(latest) == problem_key(snapshot):
        state = "ongoing"
    else:
        state = "new"
    return {"kind": "fetch_error" if snapshot.get("error") else "http_status", "text": problem_text(snapshot),
            "state": state, "since": snapshot["problem_since"], "last_good": good["date"] if good else None,
            "was_readable": latest is not None and problem_key(latest) is None}


def problem_summary(problem):
    if problem["kind"] == "http_status":
        return f"the page now returns {problem['text']}"
    return problem["text"]


# ------------------------------------------------------------------- running

def page_place(url):
    path = urllib.parse.urlsplit(url).path
    return "Homepage" if path in ("", "/") else path


def is_http_url(url):
    """True for a string like https://example.com/page."""
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def safe_fetch(fetch, url):
    """Call the fetcher. A bad URL or unexpected error becomes a skipped result, never a crash."""
    if not is_http_url(url):
        return {"skipped": f"This is not a web address starting with http:// or https://: {url}"}
    try:
        return fetch(url)
    except Exception as err:  # one bad page must not stop the whole run
        return {"skipped": f"The page could not be checked ({err})."}


def check_page(url, ignore, state_dir, today, fetch, keep=KEEP_SNAPSHOTS):
    """Fetch one page, save its snapshot and compare it with the last snapshot that had page content."""
    result = {"url": url, "place": page_place(url), "status": "unchanged", "changes": [], "notes": [],
              "previous": None, "first_check": True, "problem": None}
    slug = slug_for_url(url)
    history = load_history(state_dir, slug, today)
    latest, good = history["latest"], history["good"]
    result["first_check"], result["notes"] = not history["seen"], list(history["notes"])
    fetched = safe_fetch(fetch, url)
    if fetched.get("skipped"):
        result["status"] = "skipped"
        result["notes"].append(fetched["skipped"])
        return result
    snapshot = build_snapshot(url, fetched, today)
    if problem_key(snapshot):
        same = latest is not None and problem_key(latest) == problem_key(snapshot)
        snapshot["problem_since"] = (latest.get("problem_since") or latest["date"]) if same else today
    save_snapshot(state_dir, slug, snapshot, keep)
    if problem_key(snapshot):
        result.update(status="problem", problem=describe_problem(snapshot, latest, good))
        return result
    changes, notes = diff_snapshots(good, snapshot, ignore, latest)
    result["changes"], result["previous"] = changes, good["date"] if good else None
    if good is not None and latest["date"] != good["date"]:
        notes.append(f"Compared with {good['date']}, the last snapshot with page content. Later snapshots had none.")
    elif good is None and latest is not None:
        notes.append("No earlier snapshot of this page has page content, so there is nothing to compare yet.")
    result["notes"] += notes
    if changes:
        result["status"] = "changed"
    elif good is None:
        result["status"] = "baseline"
    return result


def run_watch(config, state_dir, fetch, today):
    """Check every page in the config. Returns one entry per competitor."""
    keep, results = config.get("keep_snapshots", KEEP_SNAPSHOTS), []
    for competitor in config["competitors"]:
        pages = [check_page(url, competitor.get("ignore", []), state_dir, today, fetch, keep)
                 for url in competitor["urls"]]
        results.append({"name": competitor["name"], "pages": pages})
    return results


READ_STATUSES = {"baseline", "changed", "unchanged"}


def any_page_read(results):
    return any(p["status"] in READ_STATUSES for c in results for p in c["pages"])


def fail(message):
    """Print a clean error and exit with status 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


def load_config(path):
    """Read and check the watchlist file."""
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        fail(f"Could not read {path}: {err}")
    if not isinstance(config, dict):
        fail("The watchlist must be a JSON object with a 'competitors' list, like config/watchlist.template.json.")
    competitors = config.get("competitors")
    if not isinstance(competitors, list) or not competitors:
        fail("The watchlist needs a 'competitors' list with at least one competitor.")
    keep = config.get("keep_snapshots", KEEP_SNAPSHOTS)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        fail(f"keep_snapshots must be a whole number of 1 or more, written without quotes, like 8. It is {json.dumps(keep)}.")
    for competitor in competitors:
        if not isinstance(competitor, dict):
            fail("Each entry in 'competitors' must be an object with a 'name' and a list of 'urls'.")
        name = competitor.get("name")
        if not isinstance(name, str) or not name.strip():
            fail("Each competitor needs a 'name' and at least one URL in 'urls'.")
        urls = competitor.get("urls")
        if isinstance(urls, str):
            fail(f"'urls' for {name} must be a list, like [{json.dumps(urls)}].")
        if not isinstance(urls, list) or not urls:
            fail(f"'urls' for {name} must be a list with at least one URL.")
        for url in urls:
            if not is_http_url(url):
                fail(f"Bad URL for {name}: {url} (it must start with http:// or https://)")
        ignore = competitor.get("ignore", [])
        if isinstance(ignore, str):
            fail(f"'ignore' for {name} must be a list, like [{json.dumps(ignore)}].")
        if not isinstance(ignore, list):
            fail(f"'ignore' for {name} must be a list of regular expressions.")
        for pattern in ignore:
            if not isinstance(pattern, str):
                fail(f"Each ignore entry for {name} must be a regular expression in quotes: {json.dumps(pattern)}")
            try:
                re.compile(pattern)
            except re.error as err:
                fail(f"Bad regex in ignore for {name}: {pattern} ({err})")
    return config


def valid_date(text):
    """True for a real date written as YYYY-MM-DD."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        datetime.date(int(text[:4]), int(text[5:7]), int(text[8:]))
    except ValueError:
        return False
    return True


# -------------------------------------------------------------------- report

SIGNAL_NAMES = {"positioning": "Positioning", "pricing": "Pricing", "copy": "Copy", "technical": "Technical"}


def lead_candidates(results):
    """Everything that can lead the report: every change, plus pages that worked last run and fail now."""
    for competitor in results:
        for page in competitor["pages"]:
            for change in page["changes"]:
                yield competitor["name"], page, change
            problem = page["problem"]
            if problem and problem["state"] == "new" and problem["was_readable"]:
                yield competitor["name"], page, {"score": SCORES[problem["kind"]], "summary": problem_summary(problem)}


def top_change(results):
    """The single highest-scoring change as (competitor, page, change), or None."""
    best = None
    for item in lead_candidates(results):
        if best is None or item[2]["score"] > best[2]["score"]:
            best = item
    return best


def look_first_line(results):
    best = top_change(results)
    if best:
        name, page, change = best
        place = "homepage" if page["place"] == "Homepage" else f"{page['place']} page"
        return f"Look at this first: {name} {place}, {change['summary']}."
    pages = [p for c in results for p in c["pages"]]
    if not any_page_read(results):
        return "Look at this first: no page could be read this run. The reasons are below."
    if all(p["first_check"] for p in pages):
        return ("Look at this first: nothing yet. This was the first run, so snapshots are saved "
                "and next week's report will show what changed.")
    problems = sum(1 for p in pages if p["status"] == "problem")
    if problems:
        return f"Look at this first: nothing changed. {plural(problems, 'page')} could not be read, see Problems below."
    return "Look at this first: nothing changed. Details are below."


def render_page(page):
    if page["previous"]:
        when = f"compared with {page['previous']}"
    else:
        when = "first snapshot" if page["first_check"] else "first snapshot with page content"
    lines = [f"### {page['place']}", f"{page['url']} ({when})", ""]
    for change in page["changes"]:
        lines.append(f"- **{SIGNAL_NAMES[change['signal']]}: {change['title']}**")
        lines += [f"  - {detail}" for detail in change["details"]]
    return lines + [""]


def problem_line(name, page):
    problem = page["problem"]
    if problem["state"] == "first":
        what = f"{problem['text']}, first seen {problem['since']} on the first check of this page"
    elif problem["state"] == "ongoing":
        what = f"still {problem['text']}, first seen {problem['since']}"
    else:
        what = f"{problem['text']}, first seen {problem['since']}"
        if problem["last_good"]:
            what += f". Last snapshot with page content: {problem['last_good']}"
    return f"- {name} {page['place']} ({page['url']}): {what}."


def render_report(results, today):
    """Build the markdown report."""
    pages = [(c["name"], p) for c in results for p in c["pages"]]

    def count(status):
        return sum(1 for _, p in pages if p["status"] == status)

    stats = f"Pages checked: {len(pages) - count('skipped')}. With changes: {count('changed')}."
    if count("problem"):
        stats += f" Problems: {count('problem')}."
    stats += f" No change: {count('unchanged')}."
    if count("baseline"):
        stats += f" First snapshot, nothing to compare yet: {count('baseline')}."
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
    sections = (("Problems", "problem", problem_line),
                ("No change", "unchanged", None),
                ("First snapshot saved", "baseline", None))
    for heading, status, describe in sections:
        matching = [(n, p) for n, p in pages if p["status"] == status]
        if matching:
            lines += [f"## {heading}", ""]
            lines += [describe(n, p) if describe else f"- {n} {p['place']}: {p['url']}" for n, p in matching]
            lines += [""]
    notes = [(n, p, note) for n, p in pages for note in p["notes"]]
    if notes:
        lines += ["## Notes", ""] + [f"- {n} {p['place']}: {note}" for n, p, note in notes] + [""]
    return "\n".join(lines).rstrip() + "\n"


def write_report(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
    write_report(out_path, report)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Snapshot competitor pages and report positioning changes.")
    ap.add_argument("--config", help="watchlist JSON file")
    ap.add_argument("--state", default="state", help="folder for snapshots (default: ./state)")
    ap.add_argument("--out", help="report path (default: report.md, or examples/sample-report.md with --demo). "
                                  "Missing folders are created.")
    ap.add_argument("--from-dir", metavar="FOLDER",
                    help="read every page in the watchlist from saved HTML in FOLDER instead of fetching. "
                         "Each page needs its own <slug>.html file, for example tallyboard.example-pricing.html "
                         "for https://tallyboard.example/pricing. Pages without a file are skipped.")
    ap.add_argument("--demo", action="store_true", help="run offline on the example fixtures")
    ap.add_argument("--date", help="snapshot date as YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)

    if args.date and not valid_date(args.date):
        fail(f"--date must be a real date written as YYYY-MM-DD, for example 2026-11-23. It is {args.date}.")
    if args.demo:
        run_demo(args.out)
        print(f"Wrote {args.out or REPO_ROOT / 'examples' / 'sample-report.md'}")
        return 0
    if not args.config:
        ap.error("--config is required unless you use --demo")
    config = load_config(args.config)
    today = args.date or datetime.date.today().isoformat()
    if args.from_dir:
        if not Path(args.from_dir).is_dir():
            fail(f"--from-dir: there is no folder named {args.from_dir}")
        fetch = dir_fetcher(args.from_dir)
    else:
        network = paced(fetch_url, DELAY_SECONDS)
        fetch = live_fetcher(RobotsChecker(network), network)
    results = run_watch(config, args.state, fetch, today)
    out = Path(args.out or "report.md")
    write_report(out, render_report(results, today))
    print(f"Wrote {out}. Snapshots are in {args.state}/")
    if not any_page_read(results):
        print("No page could be read this run. The report lists the reasons.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
