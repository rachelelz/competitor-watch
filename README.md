# competitor-watch

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

Pages checked: 5. With changes: 4. No change: 1.

## Tallyboard

### Homepage
- Positioning: headline (H1) changed
  - Was: "Project management for freelancers"
  - Now: "Project management for agencies"
- Positioning: button and link text (CTA) changed
  - Was: "Start free"
  - Now: "Book a demo"
```

The rest of the report shows Tallyboard's Pro plan going from $12 to $15 per user and its "Team" plan renamed "Agency". One competitor's features page now redirects. Another competitor's homepage is listed under "No change", even though its testimonial, footer date and customer count all changed.

## Use it on your own competitors

1. Copy `config/watchlist.template.json` to `watchlist.json` and list the companies and pages you want to watch.
2. Run it:

   ```bash
   python3 scripts/watch.py --config watchlist.json --state state --out report.md
   ```

   The first run saves a snapshot of each page. From the second run on, you get a report of what changed.
3. Run it again next week with the same `state` folder. That folder is the memory, so keep it.

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
- Snapshot. Each page is saved as a small JSON file per date. The last 8 are kept.
- Compare. Every field is compared with last week's snapshot. Date-only sentences are ignored, and you can add an `ignore` list per competitor for lines like "Trusted by 12,000 teams". Each change is labeled positioning, pricing, copy or technical.
- Rank. A fixed score table decides the "Look at this first" line: a headline change outranks a price change, which outranks a plan rename, and so on. The table sits at the top of the script if you want to reorder it.
- Brief. Claude reads the report and the snapshots and writes the short version a team would actually read.

## What it won't do

- Log in, get around bot checks, or fetch a URL that `robots.txt` disallows. It identifies itself with its own user agent and waits two seconds between requests.
- Render JavaScript. A page that builds its content in the browser gives the script almost no text, which would look like the competitor deleted their homepage. Pages under 250 visible characters get a note and no text comparison. You can save the page as HTML yourself and run with `--from-dir`.
- Tell you what a competitor is planning. The report covers what their public pages said this week and last.

The rules that spot buttons, plan names and cookie banners go by common page markup, so a site built in an unusual way can slip past them. The demo uses made-up pages. I've also run it on a real pricing page, which is where the cookie-button problem below came from.

## Privacy

Everything runs on your computer and the snapshots stay in your `state` folder. The script only contacts the pages in your watchlist. If you use the Claude Code skill, Claude reads the report and snapshots, so that text is processed by your model provider.

## What went wrong while building it

- The same price change showed up twice. On the first demo run, "$12 per user / month" to "$15 per user / month" appeared as a pricing change and again as a body copy change. Prices are now taken out of the body text before it's compared.
- One page was counted twice in the summary line. A new page that returned a 404 was listed under both "with changes" and "first snapshot". The summary now counts each page once.
- Cookie buttons count as calls to action. On a real pricing page, "No, thanks" and "Yes, that's fine" from the cookie popup were picked up as button text. They don't cause false alerts while they stay the same, but a site that changes its cookie banner will show up in the report.

## What I'd change next

- Compare with four weeks ago as well as last week, so slow drift in wording shows up.
- Pair each removed sentence with the added sentence it replaced, and highlight the changed words.
- Let each page in the watchlist name the parts of the page to watch or skip, and send the report to Slack or email.

## Files

```
SKILL.md                         instructions Claude Code follows
scripts/watch.py                 fetch, extract, snapshot, compare, report
config/watchlist.template.json   copy this to watchlist.json
examples/fixtures/               two weeks of saved pages for three made-up competitors
examples/sample-report.md        the demo output
tests/test_watch.py              37 offline tests: python3 -m unittest discover tests
```

## License

MIT. Built with Claude Code by [Rachel Elz](https://github.com/rachelelz).
