---
name: competitor-watch
description: Watch competitor web pages for positioning changes and explain what they might mean. Use when a marketer wants to track competitor homepages, pricing pages or product pages week to week, set up a watchlist, run a comparison, or turn a change report into a short brief. It fetches the pages, saves snapshots, flags changes to headlines, taglines, CTAs, prices and plan names, and ignores small noise such as dates, cookie banners and rotating testimonials.
---

# Competitor watch

A script does the fetching and comparing. You read the result and write a short brief. The script is deterministic, so the same pages always give the same report. Your part is the reading.

In the commands below, `<skill>` means the folder this SKILL.md is in, for example `~/.claude/skills/competitor-watch`. Run every command from the user's own data folder, for example `~/competitors`. `watchlist.json`, `state/` and `report.md` live there, not in the skill folder.

## 1. Set up the watchlist

1. From the data folder, copy the template: `cp <skill>/config/watchlist.template.json watchlist.json`
2. Ask which competitors to watch and which pages. Homepages, pricing pages and product pages work best.
3. Fill in `name` and `urls` for each competitor.
4. Add `ignore` regexes only for things that change every week and mean nothing, such as "Trusted by 12,000 teams". Remember that backslashes are doubled in JSON.
5. Tell the user the tool checks `robots.txt`, skips pages it disallows, and never logs in or gets around bot checks.

## 2. Run it

```
python3 <skill>/scripts/watch.py --config watchlist.json --state state --out report.md
```

- The first run only saves snapshots. Changes show from the second run on.
- Unreachable pages slow a run down, because each one waits for the 15 second timeout.
- To try it without the network: `python3 <skill>/scripts/watch.py --demo`.
- If a site renders with JavaScript, the report says the page has very little visible text. The user can save the rendered page as HTML in a folder, named like the slug in `state/` (for example `competitor.example-pricing.html`), and run with `--from-dir FOLDER`.

## 3. Write the brief

Read `report.md`. For anything flagged, read the matching snapshots in `state/<slug>/` too, so you quote exact text.

Write a "What this might mean" brief:

- Open with one sentence: the one thing to look at first. Use the report's first line.
- Then one short block per competitor that changed: what they now emphasize, who they seem to be targeting, what to watch next.
- Keep the whole brief under 250 words.

Rules:

- Describe what changed first. Label every interpretation as interpretation, with words like "this may suggest" or "one reading is".
- Never claim to know a competitor's strategy or plans. You only see public pages.
- Quote changed text exactly, with quotation marks.
- No invented numbers. Only use prices, counts and dates that appear in the report or snapshots.
- A page with no flagged change gets no commentary.
- If the report says a page was skipped, thin or unreachable, say so and do not guess what changed.
- Plain US English. Short sentences. No hype words.

Example shape:

```
Look at this first: Tallyboard changed its homepage headline from "Project management for freelancers" to "Project management for agencies".

Tallyboard. The headline, meta description and main button all changed ("Start free" to "Book a demo"). The pricing page renamed "Team" to "Agency" and moved Pro from "$12 per user/month" to "$15 per user/month". One reading: they are aiming at agencies and moving toward sales-led signup. Watch whether the Free plan stays.
```

## 4. Optional: run it weekly

Run the same command once a week, on the same weekday, with the same `--state` folder. The state folder is what makes next week's comparison possible, so keep it.

- cron (run `mkdir -p out` once): `0 8 * * 1 cd /path/to/your/data && python3 <skill>/scripts/watch.py --config watchlist.json --out out/$(date +\%F).md`
- Claude Code scheduled task: schedule a weekly task that runs the command, reads the new report and writes the brief using the rules above.

The tool keeps the last 8 snapshots per page by default. Change `keep_snapshots` in the watchlist to keep more or fewer.
