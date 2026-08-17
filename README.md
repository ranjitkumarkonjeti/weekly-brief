# weekly-brief

A small tool that writes you a one-page briefing on what's new in AI agent and
automation tooling. You run it from your laptop, once a week, and it leaves a
markdown file in the `briefings/` folder.

## What it actually does

1. Searches Hacker News for a handful of terms about AI agents and workflow
   automation.
2. Throws away anything posted more than 7 days ago.
3. Throws away anything it has already shown you before (it remembers, in
   `seen.json`).
4. Sends what's left to Google Gemini and asks it to pick the 3–5 most
   interesting ones and write two sentences on each. The model is told to use
   **only** the text it was given — no outside knowledge.
5. Visits every link to check it actually works. Dead links get dropped, not
   printed.
6. Writes `briefings/YYYY-MM-DD.md` and remembers the URLs it showed you.

## The four things it promises

**It always writes a file.** If anything goes wrong — wrong key, no internet,
the model misbehaves — you still get this week's file. It'll say `RUN FAILED`
at the top, explain the problem in plain words, and tell you the date of the
last run that worked. You never open the folder and find nothing there.

**It checks every link.** Each URL is requested with a 5-second timeout before
anything is written. If it doesn't come back healthy, it doesn't go in the file.

**It shows you the source.** Under each AI-written summary you get the real
Hacker News title and the real URL. Those come from the search results, not
from the model — so if a summary doesn't match its story, you can see that
without clicking anything.

**It's honest about a quiet week.** The header carries the real numbers, like
`3 items met the bar, out of 41 results scanned` and `4 items suppressed as
already reported`. If fewer than 3 items survive, it adds:

> Thin week. This may mean my sources are too narrow, not that nothing happened.

It will never pad the page to look busier than the week was.

## Installing it

You need a Google Gemini API key. Get one free at
<https://aistudio.google.com/apikey>.

Open Terminal, then run these three commands, one at a time, from inside this
folder:

```bash
python3 -m venv .venv
```

```bash
./.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Now open the new `.env` file in a text editor and replace
`paste-your-key-here` with your real key. Save it.

`.env` is listed in `.gitignore`, so your key will never be uploaded to GitHub.

## Running it

```bash
./.venv/bin/python weekly_brief.py
```

Your briefing appears in the `briefings/` folder, named with today's date.

### Testing without spending anything

```bash
./.venv/bin/python weekly_brief.py --dry-run
```

This does everything except call the AI — it still searches, filters, and
checks links, so you can confirm the plumbing works for free. It writes to
`YYYY-MM-DD.dry-run.md` and leaves `seen.json` alone, so a test run can't
overwrite a real briefing or use up this week's stories.

## Things you might want to change

Open `weekly_brief.py` and look at the block near the top marked
*"Settings you might reasonably want to change"*:

- `SEARCH_TERMS` — the search terms. If you keep getting thin weeks, widen
  these.
- `WINDOW_DAYS` — how far back to look. Default 7.
- `MAX_ITEMS` — the most items it will ever show. Default 5.

## About the AI model

The tool doesn't have a model name written into it. Every run, it asks Google
which models exist right now and picks the newest suitable one, so it won't go
stale as Google releases new versions. If you ever want to force a specific
model, uncomment the `GEMINI_MODEL` line in your `.env`.

## The files in here

| File | What it is |
|---|---|
| `weekly_brief.py` | The whole tool. One file. |
| `.env` | Your API key. Never committed. You create this. |
| `.env.example` | A template showing what `.env` should look like. |
| `seen.json` | What you've already been shown, plus the last good run date. Created automatically. |
| `briefings/` | Your briefings, one file per run. |
| `requirements.txt` | The one outside library this needs (`requests`). |

## If something goes wrong

Read the briefing file. That's the whole idea — the error is written there in
plain words, not just printed to a terminal you've already closed.

Common ones:

- **"No Gemini API key was found"** — you haven't created `.env`, or the line
  in it isn't spelled `GEMINI_API_KEY=...`.
- **"Google rejected the API key"** — the key is wrong or incomplete. Copy it
  again from <https://aistudio.google.com/apikey>.
- **"Could not reach Hacker News"** — you're offline.
- **"Google is rate-limiting this API key"** — the free tier has limits. Wait
  and run it again.
