# competitor-watch

[![tests](https://github.com/rachelelz/competitor-watch/actions/workflows/tests.yml/badge.svg)](https://github.com/rachelelz/competitor-watch/actions/workflows/tests.yml)

Checks your competitors' homepages, pricing pages and product pages once a week and tells you what changed in how they position themselves: a new headline, a new price, a "Start free" button that became "Book a demo", a page that now redirects. Rotating testimonials, footer dates and most cookie banner text are filtered out, so the report sticks to changes worth reading.

It's a Claude Code skill with a plain Python script underneath. The script fetches, compares and writes the report. Claude reads the report and writes a short brief on what the changes might mean.

## The problem

Keeping an eye on competitors usually means opening five tabs when someone remembers to, scrolling, and trying to recall whether that pricing page said $12 or $15 last month. A price change or a new headline can go unnoticed for weeks.

## Try it in two minutes

You need Python 3.9 or newer. Nothing to install.

```bash
git clone https://github.com/rachelelz/competitor-watch
cd competitor-watch
python3 scripts/watch.py --demo
```

The demo runs offline on two saved weeks of three made-up competitors and writes `examples/sample-report.md`. The top of it:

```text
Look at this first: Tallyboard homepage, headline (H1) changed from "Project management for freelancers" to "Project management for agencies".

# Competitor watch: 2026-11-23

Pages checked: 5. With changes: 3. Problems: 1. No change: 1.

## Tallyboard

### Homepage
https://tallyboard.example/ (compared with 2026-11-16)

- **Positioning: headline (H1) changed**
  - Was: "Project management for freelancers"
  - Now: "Project management for agencies"
- **Positioning: button and link text (CTA) changed**
  - Was: "Start free"
  - Now: "Book a demo"
```

The rest of the report lists the changes on Tallyboard's pricing page: the price "$12 per user/month" became "$15 per user/month" and the plan name "Team" became "Agency". Prices and plan names come out as two separate lists, so the report doesn't say which plan a price belongs to. Orbitask's features page now redirects. Plankit's enterprise page returns HTTP 404, so it's listed under "Problems". Plankit's homepage is listed under "No change", even though its testimonial, footer date and customer count all changed.

On top of the report, Claude writes a short brief. The start of `examples/sample-brief.md`:

```text
Look at this first: Tallyboard homepage, headline (H1) changed from "Project management for freelancers" to "Project management for agencies".

Tallyboard.
The page title and social title made the same switch from "freelancers" to "agencies".
The meta description now starts "Tallyboard gives agencies one place for client work."
A button changed from "Start free" to "Book a demo".
New section headings are "Built for agency teams" and "Capacity planning across accounts", and "Built for solo work" is gone.
```

## Use it on your own competitors

1. Make a data folder outside the repo, for example `~/competitors`, and copy the template into it. From the repo folder:

   ```bash
   mkdir -p ~/competitors
   cp config/watchlist.template.json ~/competitors/watchlist.json
   ```

   Then list the companies and pages you want to watch in `~/competitors/watchlist.json`. Each URL needs to start with `https://` or `http://`.
2. Run it from the data folder. Replace `/path/to/competitor-watch` with the folder you cloned:

   ```bash
   cd ~/competitors
   python3 /path/to/competitor-watch/scripts/watch.py --config watchlist.json --state state --out report.md
   ```

   The first run saves a snapshot of each page. From the second run on, you get a report of what changed.
3. Run it again next week from the same data folder, with the same `state` folder. That folder is the memory, so keep it.

This keeps your watchlist, snapshots and reports out of the repo. The repo's `.gitignore` lists them too, in case one ends up there.

With Claude Code, clone the repo into your skills folder and ask for it in plain words:

```bash
git clone https://github.com/rachelelz/competitor-watch ~/.claude/skills/competitor-watch
```

```text
Set up competitor-watch for me. I want to watch these three companies: ...
```

Claude helps you fill in the watchlist, runs the script, and writes a brief of under 250 words. It quotes the changed text exactly, labels its interpretation as interpretation, and never claims to know a competitor's strategy. `SKILL.md` has the full instructions, including how to run it weekly with cron or a Claude Code scheduled task.

## How it works

- Extract. For each page the script pulls out the title, meta and social descriptions, H1 to H3 headings, button and call-to-action text, prices and plan names, and the visible body text. Navigation, footers, testimonials and cookie banners are skipped at this step, because comparing whole pages would flag every rotating quote and footer date.
- Snapshot. Each page is saved as a small JSON file per date. The last 8 are kept, plus the newest one with page content, so a run of failed weeks still leaves something to compare with.
- Compare. Every field is compared with the last snapshot that had page content, which is usually last week's. If it's an older one, the report says which date it used. Date-only sentences are ignored, and you can add an `ignore` list per competitor for lines like "Trusted by 12,000 teams". The list applies to every text field except prices and plan names. Each change is labeled positioning, pricing, copy or technical. A page that returns an error or can't be reached is listed under "Problems" on every run until it works again, with the date the problem was first seen.
- Rank. A fixed score table decides the "Look at this first" line: a headline change outranks a price change, which outranks a plan rename, and so on. The table sits at the top of the script if you want to reorder it.
- Brief. Claude reads the report and the snapshots and writes the short version a team would actually read.

## What it won't do

- Log in, get around bot checks, or fetch a URL that `robots.txt` disallows. If a site's `robots.txt` returns a server error, HTTP 401 or 403, or can't be reached, that site's pages are skipped for the run. It identifies itself with its own user agent and waits two seconds before each fetch after the first, `robots.txt` included. Redirects within a fetch are followed right away.
- Render JavaScript. A page that builds its content in the browser gives the script almost no text, which would look like the competitor deleted their homepage. Pages with fewer than 250 characters of visible text get a note on every run. Their title, meta description and social tags are still compared. Their headings, buttons, prices, plan names and body text are not. As a workaround, save the pages from your browser as HTML and run with `--from-dir FOLDER`. That reads every page in the watchlist from the folder instead of the web, one `<slug>.html` file per page (`https://tallyboard.example/pricing` becomes `tallyboard.example-pricing.html`). A page without a file is skipped, so save all of them, not only the thin one.
- Tell you what a competitor is planning. The report covers what their public pages said this week and last.

The rules that spot buttons, plan names and cookie banners go by common page markup, so a site built in an unusual way can slip past them. The demo uses made-up pages. I've also run it on a real pricing page, which is where the cookie-button problem below came from.

## Privacy

Everything runs on your computer and the snapshots stay in your `state` folder. The script only requests the pages in your watchlist, each site's `robots.txt`, and any address those pages redirect to. If you use the Claude Code skill, Claude reads the report and snapshots, so that text is processed by your model provider.

## What went wrong while building it

- The same price change showed up twice. On the first demo run, "$12 per user / month" to "$15 per user / month" appeared as a pricing change and again as a body copy change. Before the body copy is compared, each price in a sentence is now swapped for a placeholder, so a sentence that differs only by its price shows up once, as a pricing change.
- One page was counted twice in the summary line. A new page that returned a 404 was listed under both "with changes" and "first snapshot". The summary now counts each page once.
- Cookie buttons count as calls to action. On a real pricing page, "No, thanks" and "Yes, that's fine" from the cookie popup were picked up as button text. They don't cause false alerts while they stay the same, but a site that changes its cookie banner will show up in the report.
- One failed week hid the next week's changes. A second code review found that after a week where a page timed out, the next run compared against that empty snapshot and skipped the text comparison, so a headline change made in between never showed up. Each run now compares with the last snapshot that has page content and says which date it used.

## What I'd change next

- Compare with four weeks ago as well as last week, so slow drift in wording shows up.
- Pair each removed sentence with the added sentence it replaced, and highlight the changed words.
- Tie each price to its plan, so the report can say which plan's price changed.

## Files

```
SKILL.md                         instructions Claude Code follows
scripts/watch.py                 fetch, extract, snapshot, compare, report
config/watchlist.template.json   copy this to watchlist.json in your data folder
examples/fixtures/               two weeks of saved pages for three made-up competitors
examples/sample-report.md        the demo output
examples/sample-brief.md         the brief Claude writes from the demo report
tests/test_watch.py              83 offline tests: python3 -m unittest discover tests
```

## License

MIT. Built with Claude Code by [Rachel Elz](https://github.com/rachelelz).
