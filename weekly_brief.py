#!/usr/bin/env python3
"""
weekly-brief
============

Finds what's new in AI agent / automation tooling on Hacker News, asks Google
Gemini to pick the most interesting few, verifies every link actually works,
and writes a one-page markdown briefing.

The four rules this tool is built around:

  1. ALWAYS write a file. If anything fails, the file still gets written with
     RUN FAILED at the top, the error in plain words, and the date of the last
     successful run. The process then exits non-zero.
  2. CHECK EVERY LINK before writing. Dead or unreachable URLs get dropped,
     never printed.
  3. SHOW THE SOURCE under each summary -- the real Hacker News title and URL,
     taken from the search results and NOT from anything the model wrote, so a
     mismatch between summary and source is visible without opening the link.
  4. BE HONEST ABOUT A THIN WEEK. Real counts in the header, an explicit
     warning when fewer than 3 items survive, and never any padding.

Usage:
    python3 weekly_brief.py
    python3 weekly_brief.py --dry-run     # skips the paid model call
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover - guidance for a non-engineer user
    sys.stderr.write(
        "The 'requests' library is not installed.\n"
        "Run this once, from inside the weekly-brief folder:\n"
        "    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt\n"
        "Then run the tool with:  ./.venv/bin/python weekly_brief.py\n"
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Settings you might reasonably want to change
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEFINGS_DIR = os.path.join(HERE, "briefings")
SEEN_PATH = os.path.join(HERE, "seen.json")
ENV_PATH = os.path.join(HERE, ".env")

# The search terms. A "handful" -- broad enough to catch things, narrow enough
# that the results are actually about agents and automation.
SEARCH_TERMS = [
    "AI agent",
    "agentic",
    "LLM agent framework",
    "workflow automation",
    "MCP server",
    "AI coding agent",
]

WINDOW_DAYS = 7           # rule: drop anything older than 7 days
MIN_ITEMS_FOR_A_GOOD_WEEK = 3   # rule: below this, say so out loud
MAX_ITEMS = 5             # ask the model for 3-5
HITS_PER_TERM = 100       # how many HN results to pull per search term

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

LINK_TIMEOUT = 5          # rule: 5-second timeout on every link check
SEARCH_TIMEOUT = 15
MODEL_TIMEOUT = 90

# Google's models sometimes come back "busy" for a few minutes at a time. Wait
# and retry rather than losing the week, then fall back to the next-best model.
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
RETRY_DELAYS = (5, 20, 45)   # seconds to wait before each retry
MODEL_FALLBACKS = 3          # how many models to keep as backups

# A normal-looking browser user agent. Without one, a fair number of sites
# return 403 to link checkers and we'd throw away perfectly good stories.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class BriefError(Exception):
    """An error we can explain to a human in one sentence."""

    def __init__(self, plain_message: str, detail: str = ""):
        super().__init__(plain_message)
        self.plain_message = plain_message
        self.detail = detail


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def log(message: str) -> None:
    """Progress output, so you can see what it's doing while it runs."""
    print("  " + message, flush=True)


def today_str() -> str:
    return dt.date.today().isoformat()


def load_env_file(path: str) -> None:
    """
    Read a .env file into the environment. Deliberately tiny: KEY=value, one
    per line, '#' starts a comment, quotes around the value are optional.
    Real environment variables win over the file.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def normalise_url(url: str) -> str:
    """
    Make two spellings of the same link compare equal, so 'already reported'
    actually means already reported. Lowercases the host, drops tracking
    parameters, drops a trailing slash and any '#fragment'.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    keep = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"ref", "ref_src", "fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    query = urllib.parse.urlencode(keep)

    path = parts.path.rstrip("/") or "/"
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


# --------------------------------------------------------------------------
# seen.json -- the memory of what you've already been shown
# --------------------------------------------------------------------------

def load_seen(path: str) -> Tuple[Dict[str, str], Optional[str]]:
    """
    Returns (seen_urls, last_successful_run_date).

    seen_urls maps normalised URL -> the date it was first reported.
    Accepts a plain list of URLs too, in case the file was hand-edited.
    A corrupt file is a warning, not a crash: we'd rather show you a repeat
    than fail the run.
    """
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        log("WARNING: seen.json could not be read, treating it as empty.")
        return {}, None

    if isinstance(data, list):
        return {normalise_url(str(u)): "" for u in data}, None

    if isinstance(data, dict):
        raw = data.get("urls", {})
        last_run = data.get("last_successful_run") or None
        if isinstance(raw, list):
            return {normalise_url(str(u)): "" for u in raw}, last_run
        if isinstance(raw, dict):
            return (
                {normalise_url(str(k)): str(v) for k, v in raw.items()},
                last_run,
            )
    return {}, None


def save_seen(
    path: str, seen: Dict[str, str], last_successful_run: Optional[str]
) -> None:
    payload = {
        "last_successful_run": last_successful_run,
        "urls": seen,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)  # atomic: never leave a half-written seen.json


# --------------------------------------------------------------------------
# Step 1 + 2: search Hacker News, drop anything older than 7 days
# --------------------------------------------------------------------------

def search_hacker_news(
    session: requests.Session, terms: List[str], window_days: int
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Returns (results, warnings).

    One search per term. Results are de-duplicated by URL across terms, so
    'scanned' is a count of distinct stories, not of API rows.
    """
    cutoff = int(time.time()) - window_days * 24 * 60 * 60
    by_url: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    successful_terms = 0

    for term in terms:
        params = {
            "query": term,
            "tags": "story",
            "hitsPerPage": HITS_PER_TERM,
            # Ask the API for the window too -- fewer wasted rows over the wire.
            "numericFilters": "created_at_i>{0}".format(cutoff),
        }
        try:
            response = session.get(
                HN_SEARCH_URL, params=params, timeout=SEARCH_TIMEOUT
            )
            response.raise_for_status()
            hits = response.json().get("hits", [])
        except (requests.RequestException, ValueError) as exc:
            warnings.append(
                'The search for "{0}" failed ({1}), so this briefing may be '
                "missing stories.".format(term, type(exc).__name__)
            )
            log('search "{0}": FAILED'.format(term))
            continue

        successful_terms += 1
        kept = 0
        for hit in hits:
            created = hit.get("created_at_i")
            if not isinstance(created, int) or created < cutoff:
                continue  # step 2: older than the window

            title = (hit.get("title") or "").strip()
            if not title:
                continue

            url = (hit.get("url") or "").strip()
            if not url:
                # Ask HN / Show HN text posts have no external link. The HN
                # discussion page is the real, working source for those.
                url = HN_ITEM_URL.format(id=hit.get("objectID"))

            key = normalise_url(url)
            if key in by_url:
                continue
            by_url[key] = {
                "title": title,
                "url": url,
                "key": key,
                "points": hit.get("points") or 0,
                "comments": hit.get("num_comments") or 0,
                "created_at": created,
                "hn_discussion": HN_ITEM_URL.format(id=hit.get("objectID")),
                "matched_term": term,
            }
            kept += 1
        log('search "{0}": {1} in the last {2} days'.format(term, kept, window_days))

    if successful_terms == 0:
        raise BriefError(
            "Could not reach Hacker News. Every search failed, which usually "
            "means there is no internet connection right now.",
            "; ".join(warnings),
        )

    results = sorted(by_url.values(), key=lambda r: r["created_at"], reverse=True)
    return results, warnings


# --------------------------------------------------------------------------
# Step 4: ask Gemini to choose and summarise
# --------------------------------------------------------------------------

# Model families that are not plain text-in / text-out, and so are wrong for
# this job no matter how new they are.
_NON_TEXT_HINTS = (
    "embedding", "aqa", "image", "tts", "audio", "live", "veo", "imagen",
    "vision", "learnlm", "gemma", "translate", "robotics", "computer-use",
)


def _score_model(model_id: str) -> Optional[Tuple[Any, ...]]:
    """
    Rank a Gemini model id. Higher sorts better. Returns None for models that
    are unsuitable for this task.

    This is how we avoid hardcoding a model name that goes stale: we read
    Google's live list and score whatever is on it today.
    """
    name = model_id.lower()
    if not name.startswith("gemini"):
        return None
    if any(hint in name for hint in _NON_TEXT_HINTS):
        return None

    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    if not match:
        return None
    version = (int(match.group(1)), int(match.group(2) or 0))

    is_stable = not any(
        tag in name for tag in ("preview", "exp", "experimental", "-rc")
    )

    # flash is the right shape for a weekly summariser: fast and cheap.
    if "flash-lite" in name or "lite" in name:
        tier = 1
    elif "flash" in name:
        tier = 3
    elif "pro" in name:
        tier = 2
    else:
        tier = 0

    # Prefer a plain 'gemini-3.7-flash' over a dated snapshot of the same thing.
    undated = 1 if not re.search(r"\d{2}-\d{4}|\d{6}|\d{3}$", name) else 0

    return (1 if is_stable else 0, version, tier, undated)


def rank_models(session: requests.Session, api_key: str) -> List[str]:
    """
    Ask Google which models exist right now and rank them for this job, best
    first. We keep a few, not just one: popular models sometimes run hot and
    return "try again later", and a second choice beats no briefing.

    Set GEMINI_MODEL in .env to override.
    """
    override = os.environ.get("GEMINI_MODEL", "").strip()
    if override:
        log("using the model from .env: {0}".format(override))
        return [override]

    try:
        response = session.get(
            "{0}/models".format(GEMINI_BASE),
            params={"key": api_key, "pageSize": 200},
            timeout=SEARCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise BriefError(
            "Could not reach Google to ask which AI models are available. "
            "This usually means the internet connection is down.",
            "{0}: {1}".format(type(exc).__name__, exc),
        )

    if response.status_code in (400, 401, 403):
        raise BriefError(
            "Google rejected the API key. Open the .env file in the "
            "weekly-brief folder and check that GEMINI_API_KEY is your real "
            "key, copied in full with no spaces. You can get a key from "
            "https://aistudio.google.com/apikey",
            "HTTP {0}: {1}".format(response.status_code, response.text[:300]),
        )
    if response.status_code >= 400:
        raise BriefError(
            "Google's service returned an error (HTTP {0}) when asked which "
            "models are available.".format(response.status_code),
            response.text[:300],
        )

    try:
        models = response.json().get("models", [])
    except ValueError:
        raise BriefError(
            "Google sent back a model list that could not be read.",
            response.text[:300],
        )

    scored = []
    for model in models:
        model_id = str(model.get("name", "")).replace("models/", "")
        methods = model.get("supportedGenerationMethods") or model.get(
            "supportedActions"
        )
        if methods and "generateContent" not in methods:
            continue
        score = _score_model(model_id)
        if score is None:
            continue
        scored.append((score, model_id))

    if not scored:
        raise BriefError(
            "Google's model list came back, but none of the models on it can "
            "write text summaries. Set GEMINI_MODEL in your .env file to pick "
            "one by hand.",
            "{0} models offered".format(len(models)),
        )

    scored.sort(reverse=True)
    ranked = [model_id for _, model_id in scored[:MODEL_FALLBACKS]]
    log("newest suitable model on Google's list: {0}".format(ranked[0]))
    if len(ranked) > 1:
        log("(backups if it is busy: {0})".format(", ".join(ranked[1:])))
    return ranked


def build_prompt(candidates: List[Dict[str, Any]], max_items: int) -> str:
    lines = [
        "You are helping compile a weekly briefing on AI agent and workflow",
        "automation tooling.",
        "",
        "Below is a numbered list of Hacker News stories from the past week.",
        "",
        "Choose the {0} to {1} most interesting and consequential items.".format(
            MIN_ITEMS_FOR_A_GOOD_WEEK, max_items
        ),
        "Prefer concrete releases, tools, and technical write-ups over opinion",
        "pieces and funding news.",
        "",
        "STRICT RULES:",
        "- Use ONLY the text provided below. Do not use anything you know from",
        "  outside this list. If the title is all you have, summarise only what",
        "  the title supports.",
        "- Do not invent details, version numbers, companies, or capabilities.",
        "- Write exactly two sentences for each item you choose.",
        "- If fewer than {0} items are genuinely worth reporting, return fewer.".format(
            MIN_ITEMS_FOR_A_GOOD_WEEK
        ),
        "  Do not pad the list.",
        "",
        "Reply with JSON only, in exactly this shape:",
        '{"picks": [{"id": <number>, "summary": "<two sentences>"}]}',
        "",
        "STORIES:",
    ]
    for index, item in enumerate(candidates, start=1):
        age_days = max(0, int((time.time() - item["created_at"]) // 86400))
        lines.append(
            "{0}. {1} (points: {2}, comments: {3}, posted {4} day(s) ago)".format(
                index, item["title"], item["points"], item["comments"], age_days
            )
        )
    return "\n".join(lines)


def _extract_text(payload: Dict[str, Any]) -> str:
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise BriefError(
            "The AI model refused to answer (it blocked the request). Nothing "
            "was summarised this week.",
            "blockReason={0}".format(feedback.get("blockReason")),
        )
    candidates = payload.get("candidates") or []
    if not candidates:
        raise BriefError(
            "The AI model returned an empty answer.", json.dumps(payload)[:500]
        )
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise BriefError(
            "The AI model returned an answer with no text in it.",
            "finishReason={0}".format(candidates[0].get("finishReason")),
        )
    return text


def _parse_picks(text: str, candidate_count: int) -> List[Dict[str, Any]]:
    """Parse the model's JSON, tolerating ``` fences and stray prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None
    if data is None:
        raise BriefError(
            "The AI model's answer was not in the format this tool expects, so "
            "nothing could be read from it.",
            cleaned[:500],
        )

    raw_picks = data.get("picks") if isinstance(data, dict) else data
    if not isinstance(raw_picks, list):
        raise BriefError(
            "The AI model's answer did not contain a list of picks.",
            cleaned[:500],
        )

    picks, used_ids = [], set()
    for entry in raw_picks:
        if not isinstance(entry, dict):
            continue
        try:
            item_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        summary = str(entry.get("summary", "")).strip()
        # Silently ignore an id the model invented, and any duplicate. The
        # briefing only ever shows stories that were really in the search.
        if not summary or item_id in used_ids or not 1 <= item_id <= candidate_count:
            continue
        used_ids.add(item_id)
        picks.append({"id": item_id, "summary": summary})
    return picks


class TransientModelError(Exception):
    """Google is busy or briefly broken. Worth trying again."""


def _call_model_once(
    session: requests.Session, api_key: str, model: str, body: Dict[str, Any]
) -> Dict[str, Any]:
    url = "{0}/models/{1}:generateContent".format(GEMINI_BASE, model)
    try:
        response = session.post(
            url, params={"key": api_key}, json=body, timeout=MODEL_TIMEOUT
        )
    except requests.Timeout:
        raise TransientModelError("no response within {0}s".format(MODEL_TIMEOUT))
    except requests.RequestException as exc:
        raise BriefError(
            "Could not reach the Google Gemini service. This usually means "
            "the internet connection dropped.",
            "{0}: {1}".format(type(exc).__name__, exc),
        )

    if response.status_code in (401, 403):
        raise BriefError(
            "Google rejected the API key when asked to write summaries. Check "
            "GEMINI_API_KEY in your .env file -- it may be wrong, expired, or "
            "not enabled for this API.",
            "HTTP {0}: {1}".format(response.status_code, response.text[:300]),
        )
    if response.status_code in TRANSIENT_STATUSES:
        raise TransientModelError(
            "HTTP {0}: {1}".format(response.status_code, response.text[:200])
        )
    if response.status_code >= 400:
        raise BriefError(
            "The Google Gemini service returned an error (HTTP {0}).".format(
                response.status_code
            ),
            response.text[:300],
        )

    try:
        return response.json()
    except ValueError:
        raise BriefError(
            "The Google Gemini service sent back something that was not "
            "readable data.",
            response.text[:300],
        )


def summarise_with_gemini(
    session: requests.Session,
    api_key: str,
    models: List[str],
    candidates: List[Dict[str, Any]],
    max_items: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Returns (chosen_items, model_that_worked).

    Crucially, the title and URL attached to each pick come from OUR search
    results, matched by number -- never from the model's own output. The model
    can only choose; it cannot supply a link.

    Busy models are retried with a growing pause, then we fall back to the
    next-best model, because "Google was briefly busy" is not a good enough
    reason to lose a week.
    """
    prompt = build_prompt(candidates, max_items)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    last_problem = ""
    for model_index, model in enumerate(models):
        if model_index:
            log("trying the next model instead: {0}".format(model))
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            try:
                payload = _call_model_once(session, api_key, model, body)
            except TransientModelError as exc:
                last_problem = "{0}: {1}".format(model, exc)
                if attempt < len(RETRY_DELAYS):
                    log(
                        "{0} is busy, waiting {1}s and trying again "
                        "(attempt {2} of {3})...".format(
                            model, delay, attempt, len(RETRY_DELAYS)
                        )
                    )
                    time.sleep(delay)
                continue

            picks = _parse_picks(_extract_text(payload), len(candidates))
            chosen = []
            for pick in picks[:max_items]:
                item = dict(candidates[pick["id"] - 1])
                item["summary"] = pick["summary"]
                chosen.append(item)
            return chosen, model

    raise BriefError(
        "Google's AI service is too busy right now. The tool waited and tried "
        "{0} times across {1} model(s), and every attempt came back busy. "
        "This is a problem on Google's end, not with your setup -- running the "
        "tool again in an hour usually works.".format(len(RETRY_DELAYS), len(models)),
        last_problem,
    )


def pick_without_model(
    candidates: List[Dict[str, Any]], max_items: int
) -> List[Dict[str, Any]]:
    """--dry-run stand-in: most-discussed first, no summaries invented."""
    ranked = sorted(
        candidates, key=lambda r: (r["points"], r["comments"]), reverse=True
    )
    chosen = []
    for item in ranked[:max_items]:
        copy = dict(item)
        copy["summary"] = (
            "_(dry run: no summary was generated, because the model call was "
            "skipped.)_"
        )
        chosen.append(copy)
    return chosen


# --------------------------------------------------------------------------
# Step 5: check every link
# --------------------------------------------------------------------------

def link_is_alive(session: requests.Session, url: str) -> Tuple[bool, str]:
    """
    Try HEAD first (cheap). Plenty of servers dislike HEAD, so fall back to a
    GET before believing a link is dead. Anything that isn't a good response
    within the timeout gets dropped.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    try:
        response = session.head(
            url, timeout=LINK_TIMEOUT, allow_redirects=True, headers=headers
        )
        if response.status_code in (405, 403, 401, 501, 400, 404, 429):
            response = session.get(
                url, timeout=LINK_TIMEOUT, allow_redirects=True, headers=headers,
                stream=True,
            )
            response.close()
        if response.status_code < 400:
            return True, "HTTP {0}".format(response.status_code)
        return False, "HTTP {0}".format(response.status_code)
    except requests.Timeout:
        return False, "no response within {0} seconds".format(LINK_TIMEOUT)
    except requests.RequestException as exc:
        return False, type(exc).__name__


def verify_links(
    session: requests.Session, items: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    alive, dead = [], []
    for item in items:
        ok, reason = link_is_alive(session, item["url"])
        log("link check: {0}  <- {1}".format("OK  " if ok else "DEAD", item["url"]))
        if ok:
            alive.append(item)
        else:
            item["dead_reason"] = reason
            dead.append(item)
    return alive, dead


# --------------------------------------------------------------------------
# Step 6: write the briefing
# --------------------------------------------------------------------------

def render_briefing(
    date_str: str,
    items: List[Dict[str, Any]],
    scanned: int,
    suppressed: int,
    dropped_dead: int,
    warnings: List[str],
    dry_run: bool,
    model_used: Optional[str],
) -> str:
    lines = ["# Weekly brief -- {0}".format(date_str), ""]
    if dry_run:
        lines += [
            "> **Dry run.** The AI model was not called, so the text below is "
            "not a summary. Everything else -- search, filtering, link "
            "checking -- ran for real.",
            "",
        ]

    # Rule 4: the real numbers, always, in the header.
    lines += [
        "{0} item{1} met the bar, out of {2} result{3} scanned".format(
            len(items), "" if len(items) == 1 else "s",
            scanned, "" if scanned == 1 else "s",
        ),
        "",
        "{0} item{1} suppressed as already reported".format(
            suppressed, "" if suppressed == 1 else "s"
        ),
        "",
    ]
    if dropped_dead:
        lines += [
            "{0} item{1} dropped because the link did not work".format(
                dropped_dead, "" if dropped_dead == 1 else "s"
            ),
            "",
        ]

    if len(items) < MIN_ITEMS_FOR_A_GOOD_WEEK:
        lines += [
            "Thin week. This may mean my sources are too narrow, not that "
            "nothing happened.",
            "",
        ]

    for warning in warnings:
        lines += ["> Note: {0}".format(warning), ""]

    lines.append("---")
    lines.append("")

    if not items:
        lines += [
            "No items survived this week's filters.",
            "",
        ]
    for number, item in enumerate(items, start=1):
        lines.append("## {0}. {1}".format(number, item["title"]))
        lines.append("")
        lines.append(item["summary"])
        lines.append("")
        # Rule 3: the real source, straight from the search result, so you can
        # spot a summary that doesn't match its story.
        lines.append("**Source:** {0}".format(item["title"]))
        lines.append("")
        lines.append("**Link:** <{0}>".format(item["url"]))
        lines.append("")
        lines.append(
            "*{0} points, {1} comments on Hacker News -- [discussion]({2})*".format(
                item["points"], item["comments"], item["hn_discussion"]
            )
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "*Sources searched: Hacker News, terms: {0}. Window: last {1} days. "
        "Every link above returned a good response when checked.*".format(
            ", ".join('"{0}"'.format(t) for t in SEARCH_TERMS), WINDOW_DAYS
        )
    )
    if model_used:
        lines.append("")
        lines.append("*Summaries written by: {0}*".format(model_used))
    lines.append("")
    return "\n".join(lines)


def render_failure(
    date_str: str, error: BriefError, last_success: Optional[str]
) -> str:
    return "\n".join(
        [
            "# RUN FAILED -- {0}".format(date_str),
            "",
            "This week's briefing could not be produced.",
            "",
            "**What went wrong:** {0}".format(error.plain_message),
            "",
            "**Last successful run:** {0}".format(
                last_success if last_success else "never (this tool has not "
                "completed a successful run yet)"
            ),
            "",
            "Nothing was added to `seen.json`, so anything missed this week "
            "can still turn up in the next run.",
            "",
            "---",
            "",
            "<details><summary>Technical detail (for support)</summary>",
            "",
            "```",
            (error.detail or "no further detail").strip()[:4000],
            "```",
            "",
            "</details>",
            "",
        ]
    )


def write_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# --------------------------------------------------------------------------
# The run itself
# --------------------------------------------------------------------------

def run(dry_run: bool) -> int:
    date_str = today_str()
    seen, last_success = load_seen(SEEN_PATH)
    filename = "{0}.dry-run.md".format(date_str) if dry_run else "{0}.md".format(date_str)
    out_path = os.path.join(BRIEFINGS_DIR, filename)

    session = requests.Session()

    try:
        log("Step 1: searching Hacker News ({0} terms)...".format(len(SEARCH_TERMS)))
        results, warnings = search_hacker_news(session, SEARCH_TERMS, WINDOW_DAYS)
        scanned = len(results)
        log("Steps 1-2: {0} distinct stories from the last {1} days.".format(
            scanned, WINDOW_DAYS))

        log("Step 3: removing anything already reported...")
        fresh = [item for item in results if item["key"] not in seen]
        suppressed = scanned - len(fresh)
        log("{0} suppressed as already reported, {1} left.".format(
            suppressed, len(fresh)))

        model_used = None
        if not fresh:
            chosen = []
            log("Nothing new to summarise, so the model was not called.")
        elif dry_run:
            log("Step 4: SKIPPED (dry run -- no model call, no cost).")
            chosen = pick_without_model(fresh, MAX_ITEMS)
        else:
            log("Step 4: asking Google Gemini to choose the best few...")
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                raise BriefError(
                    "No Gemini API key was found. Create a file called .env in "
                    "the weekly-brief folder containing the line: "
                    "GEMINI_API_KEY=your-key-here",
                    "checked .env and the environment for GEMINI_API_KEY",
                )
            models = rank_models(session, api_key)
            chosen, model_used = summarise_with_gemini(
                session, api_key, models, fresh, MAX_ITEMS
            )
            log("{0} chose {1} item(s).".format(model_used, len(chosen)))

        log("Step 5: checking every link ({0} to check)...".format(len(chosen)))
        alive, dead = verify_links(session, chosen)
        if dead:
            log("{0} link(s) dropped as dead or unreachable.".format(len(dead)))

        log("Step 6: writing the briefing...")
        text = render_briefing(
            date_str=date_str,
            items=alive,
            scanned=scanned,
            suppressed=suppressed,
            dropped_dead=len(dead),
            warnings=warnings,
            dry_run=dry_run,
            model_used=model_used,
        )
        write_file(out_path, text)

        if dry_run:
            log("Dry run: seen.json was left untouched.")
        else:
            for item in alive:
                seen[item["key"]] = date_str
            save_seen(SEEN_PATH, seen, date_str)
            log("seen.json updated with {0} new URL(s).".format(len(alive)))

        print("")
        print("Done. Wrote {0}".format(out_path))
        if len(alive) < MIN_ITEMS_FOR_A_GOOD_WEEK:
            print("Thin week -- the briefing says so at the top.")
        return 0

    except BriefError as error:
        # Rule 1: a file gets written no matter what.
        write_file(out_path, render_failure(date_str, error, last_success))
        sys.stderr.write("\nRUN FAILED: {0}\n".format(error.plain_message))
        sys.stderr.write("Wrote the failure to {0}\n".format(out_path))
        return 1

    except Exception as unexpected:  # anything we did not anticipate
        error = BriefError(
            "The tool hit an unexpected problem: {0}".format(unexpected),
            traceback.format_exc(),
        )
        write_file(out_path, render_failure(date_str, error, last_success))
        sys.stderr.write("\nRUN FAILED: {0}\n".format(error.plain_message))
        sys.stderr.write("Wrote the failure to {0}\n".format(out_path))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write this week's AI agent / automation briefing."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the Gemini call (free). Writes YYYY-MM-DD.dry-run.md and "
        "does not update seen.json.",
    )
    args = parser.parse_args()

    load_env_file(ENV_PATH)
    print("weekly-brief -- {0}{1}".format(
        today_str(), "  (dry run)" if args.dry_run else ""))
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
