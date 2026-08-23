#!/usr/bin/env python3
"""
generate_report.py - build a cloud cost savings report from a billing export.

Reads a JSON billing file, checks that the numbers add up, works out where the
savings are, and writes a single self-contained HTML file.

Two rules govern this script, and they are the same two that govern weekly-brief:

  1. It always writes a file. If anything fails, it writes a report that says
     FAILED at the top and explains why, then exits non-zero. It never
     finishes silently, because a missing report looks exactly like a month
     nobody looked at.

  2. It refuses to publish numbers that do not reconcile. If the category
     totals, the team totals and the invoiced total disagree, the run fails
     rather than quietly rendering a plausible-looking page.

Usage:
    python generate_report.py
    python generate_report.py --data data/august-2026.json --out output/report.html
"""

import argparse
import datetime as dt
import html
import json
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "data" / "august-2026.json"
DEFAULT_OUT = HERE / "output" / "cloud-cost-report.html"

# A category or team total may differ from the sum of its parts by at most this
# much before the run is treated as a failure. Rounding in a billing export is
# normal; a real mismatch is not.
TOLERANCE = 1.0


# --------------------------------------------------------------------------
# loading and checking
# --------------------------------------------------------------------------

class DataProblem(Exception):
    """Raised when the billing data does not hold together."""


def load(path):
    if not path.exists():
        raise DataProblem("No billing file at %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise DataProblem("The billing file is not valid JSON: %s" % exc)


def check(data):
    """Return a list of human-readable problems. Empty list means it adds up."""
    problems = []

    for key in ("period", "invoiced_total", "prior_total", "categories", "teams", "resources"):
        if key not in data:
            problems.append("Missing '%s' in the billing file." % key)
    if problems:
        return problems

    total = data["invoiced_total"]

    cat_sum = sum(c["cost"] for c in data["categories"])
    if abs(cat_sum - total) > TOLERANCE:
        problems.append(
            "Categories add up to $%s but the invoiced total is $%s (out by $%s)."
            % (f"{cat_sum:,}", f"{total:,}", f"{abs(cat_sum - total):,}")
        )

    team_sum = sum(t["cost"] for t in data["teams"])
    if abs(team_sum - total) > TOLERANCE:
        problems.append(
            "Teams add up to $%s but the invoiced total is $%s (out by $%s)."
            % (f"{team_sum:,}", f"{total:,}", f"{abs(team_sum - total):,}")
        )

    cat_prior = sum(c["prior"] for c in data["categories"])
    team_prior = sum(t["prior"] for t in data["teams"])
    if abs(cat_prior - team_prior) > TOLERANCE:
        problems.append(
            "Last month's categories ($%s) and teams ($%s) disagree."
            % (f"{cat_prior:,}", f"{team_prior:,}")
        )
    if abs(cat_prior - data["prior_total"]) > TOLERANCE:
        problems.append(
            "Last month's parts add up to $%s but prior_total says $%s."
            % (f"{cat_prior:,}", f"{data['prior_total']:,}")
        )

    known_teams = {t["name"] for t in data["teams"]}
    known_cats = {c["name"] for c in data["categories"]}
    for r in data["resources"]:
        if r["team"] not in known_teams:
            problems.append("Resource %s is tagged to unknown team '%s'." % (r["id"], r["team"]))
        if r["category"] not in known_cats:
            problems.append("Resource %s has unknown category '%s'." % (r["id"], r["category"]))
        if r["cost"] > total:
            problems.append("Resource %s costs more than the whole invoice." % r["id"])
        if r.get("saving", 0) > r["cost"]:
            problems.append(
                "Resource %s claims a saving larger than its own cost. "
                "A saving can never exceed what is being spent." % r["id"]
            )

    return problems


# --------------------------------------------------------------------------
# working out the numbers
# --------------------------------------------------------------------------

def pct(now, before):
    if not before:
        return 0.0
    return (now - before) / before * 100.0


def summarise(data):
    total = data["invoiced_total"]
    prior = data["prior_total"]
    resources = sorted(data["resources"], key=lambda r: -r["cost"])

    teams = []
    for t in data["teams"]:
        teams.append(dict(t, delta=t["cost"] - t["prior"], pct=pct(t["cost"], t["prior"])))
    teams.sort(key=lambda t: -t["cost"])

    cats = []
    for c in data["categories"]:
        cats.append(dict(c, delta=c["cost"] - c["prior"], pct=pct(c["cost"], c["prior"])))
    cats.sort(key=lambda c: -c["cost"])

    increase = total - prior
    risers = sorted([t for t in teams if t["delta"] > 0], key=lambda t: -t["delta"])
    top_riser = risers[0] if risers else None

    saving = sum(r.get("saving", 0) for r in resources)
    actionable = [r for r in resources if r.get("saving", 0) > 0]
    actionable.sort(key=lambda r: -r["saving"])

    return {
        "period": data["period"],
        "total": total,
        "prior": prior,
        "increase": increase,
        "increase_pct": pct(total, prior),
        "categories": cats,
        "teams": teams,
        "resources": resources,
        "top_riser": top_riser,
        "top_riser_share": (top_riser["delta"] / increase * 100.0) if top_riser and increase else 0.0,
        "risers": risers,
        "saving": saving,
        "saving_pct": saving / total * 100.0 if total else 0.0,
        "actionable": actionable,
        "top_resources_sum": sum(r["cost"] for r in resources),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=False)


def money(n):
    return "$%s" % f"{round(n):,}"


def signed_money(n):
    return ("+" if n >= 0 else "\u2212") + "$%s" % f"{abs(round(n)):,}"


def signed_pct(p):
    return ("+" if p >= 0 else "\u2212") + "%.1f%%" % abs(p)


def tone(n):
    """Spend going up is bad, so up is red and down is green."""
    if round(n, 1) > 0:
        return "up"
    if round(n, 1) < 0:
        return "down"
    return "flat"


def bar(value, biggest, width=300, hot=False):
    px = max(3, round(value / biggest * width)) if biggest else 3
    cls = "bar hot" if hot else "bar"
    return '<div class="%s" style="width:%dpx">&nbsp;</div>' % (cls, px)


def code_block(block):
    if not block:
        return ""
    return (
        '<div class="codewrap"><div class="codelabel">%s</div>'
        '<pre class="code"><code>%s</code></pre></div>'
        % (esc(block["label"]), esc(block["code"]))
    )


def render_resource_detail(r):
    maths = "".join("<li>%s</li>" % esc(m) for m in r.get("maths", []))
    change = code_block(r.get("change"))
    change_html = change if change else (
        '<p class="nochange">No change proposed.</p>'
    )
    saving_line = (
        '<span class="savepill">Saves %s a month</span>' % money(r["saving"])
        if r.get("saving") else '<span class="savepill zero">No saving identified</span>'
    )
    return """
      <tr class="detail">
        <td colspan="6">
          <div class="panel">
            <div class="panel-head">%(saving)s<span class="conf">%(conf)s confidence</span></div>
            <p class="finding">%(finding)s</p>
            <div class="cols">
              <div class="col">%(current)s</div>
              <div class="col">%(change)s</div>
            </div>
            <div class="cols">
              <div class="col">
                <h4>How the number is worked out</h4>
                <ul class="maths">%(maths)s</ul>
              </div>
              <div class="col">
                <h4>What could go wrong</h4>
                <p class="risk">%(risk)s</p>
                <h4>Effort</h4>
                <p class="risk">%(effort)s</p>
              </div>
            </div>
          </div>
        </td>
      </tr>""" % {
        "saving": saving_line,
        "conf": esc(r.get("confidence", "unknown").title()),
        "finding": esc(r["finding"]),
        "current": code_block(r.get("current_config")),
        "change": change_html,
        "maths": maths,
        "risk": esc(r.get("risk", "")),
        "effort": esc(r.get("effort", "")),
    }


STACK_COLOURS = ["#9E3A26", "#C2714F", "#3C4550", "#767C84", "#B9BEC4", "#D6D8DA"]


def render(s, generated_at):
    biggest_cat = max(c["cost"] for c in s["categories"])
    biggest_team = max(t["cost"] for t in s["teams"])
    biggest_save = max([r["saving"] for r in s["actionable"]], default=1)

    cat_rows = "".join(
        """
    <tr>
      <td class="nm">%s<em>%.1f%% of spend</em></td>
      <td class="barcell">%s</td>
      <td class="cost">%s</td>
      <td class="pct %s">%s</td>
    </tr>""" % (
            esc(c["name"]), c["cost"] / s["total"] * 100.0,
            bar(c["cost"], biggest_cat, hot=(c["name"] == "AI / ML")),
            money(c["cost"]), tone(c["pct"]), signed_pct(c["pct"]),
        )
        for c in s["categories"]
    )

    team_rows = "".join(
        """
    <tr%s>
      <td class="nm">%s<em>%s</em></td>
      <td class="barcell">%s</td>
      <td class="cost">%s</td>
      <td class="dols %s">%s</td>
      <td class="pct %s">%s</td>
    </tr>""" % (
            ' class="flag"' if s["top_riser"] and t["name"] == s["top_riser"]["name"] else "",
            esc(t["name"]), esc(t["top_resource"]),
            bar(t["cost"], biggest_team,
                hot=bool(s["top_riser"] and t["name"] == s["top_riser"]["name"])),
            money(t["cost"]),
            tone(t["delta"]), signed_money(t["delta"]),
            tone(t["pct"]), signed_pct(t["pct"]),
        )
        for t in s["teams"]
    )

    riser_total = sum(t["delta"] for t in s["risers"]) or 1
    stack_cells = "".join(
        '<td style="width:%.1f%%;background:%s"></td>'
        % (t["delta"] / riser_total * 100.0, STACK_COLOURS[i % len(STACK_COLOURS)])
        for i, t in enumerate(s["risers"])
    )
    stack_keys = "".join(
        '<td><span class="sw" style="background:%s"></span>%s &nbsp;%s</td>'
        % (STACK_COLOURS[i % len(STACK_COLOURS)], esc(t["name"]), money(t["delta"]))
        for i, t in enumerate(s["risers"])
    )

    res_rows = []
    for r in s["resources"]:
        res_rows.append(
            """
      <tr class="row%s">
        <td class="res"><span class="tw">%s</span>%s<em>%s</em></td>
        <td class="opt"><span class="cloud">%s</span></td>
        <td class="opt">%s</td>
        <td class="num">%s</td>
        <td class="num %s">%s</td>
        <td class="num save">%s</td>
      </tr>""" % (
                " flag" if r.get("flag") else "",
                "&#9656;", esc(r["id"]), esc(r["description"]),
                esc(r["cloud"]), esc(r["team"]),
                money(r["cost"]),
                tone(pct(r["cost"], r["prior"])), signed_pct(pct(r["cost"], r["prior"])),
                money(r["saving"]) if r.get("saving") else "&mdash;",
            )
        )
        res_rows.append(render_resource_detail(r))
    res_rows = "".join(res_rows)

    action_rows = "".join(
        """
    <tr>
      <td class="nm">%s<em>%s &middot; %s confidence</em></td>
      <td class="barcell">%s</td>
      <td class="cost">%s</td>
      <td class="pct">%.0f%% of it</td>
    </tr>""" % (
            esc(r["id"]), esc(r["team"]), esc(r.get("confidence", "?")),
            bar(r["saving"], biggest_save),
            money(r["saving"]),
            r["saving"] / r["cost"] * 100.0,
        )
        for r in s["actionable"]
    )

    counted = (
        "%d of %d resources have an identified saving. "
        "The other %d are listed with a dash, which is deliberate: "
        "a report that finds something wrong with everything is not being honest."
        % (len(s["actionable"]), len(s["resources"]),
           len(s["resources"]) - len(s["actionable"]))
    )

    return PAGE % {
        "period": esc(s["period"]),
        "total": money(s["total"]),
        "increase_pct": signed_pct(s["increase_pct"]),
        "increase_tone": tone(s["increase_pct"]),
        "increase": money(s["increase"]),
        "riser_name": esc(s["top_riser"]["name"]) if s["top_riser"] else "n/a",
        "riser_share": "%.0f%%" % s["top_riser_share"],
        "saving": money(s["saving"]),
        "saving_pct": "%.1f%%" % s["saving_pct"],
        "saving_year": money(s["saving"] * 12),
        "cat_rows": cat_rows,
        "team_rows": team_rows,
        "stack_cells": stack_cells,
        "stack_keys": stack_keys,
        "increase_money": money(s["increase"]),
        "res_rows": res_rows,
        "action_rows": action_rows,
        "counted": counted,
        "top_sum": money(s["top_resources_sum"]),
        "top_share": "%.0f%%" % (s["top_resources_sum"] / s["total"] * 100.0),
        "generated": esc(generated_at),
    }


def render_failure(reasons, generated_at, last_good):
    items = "".join("<li>%s</li>" % esc(r) for r in reasons)
    return FAIL_PAGE % {
        "items": items,
        "generated": esc(generated_at),
        "last_good": esc(last_good or "no successful run on record"),
    }


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

STYLE = """
  *{box-sizing:border-box}
  body{margin:0;background:#FBFBF9;color:#12161C;
    font-family:"Public Sans","Helvetica Neue",Arial,sans-serif;
    font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1040px;margin:0 auto;padding:0 28px 72px}
  header{border-bottom:2px solid #12161C;padding:40px 0 22px;margin-bottom:18px}
  .eyebrow{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:11px;
    letter-spacing:.14em;text-transform:uppercase;color:#666E77;margin:0 0 14px}
  h1{font-family:"Archivo","Helvetica Neue",Arial,sans-serif;font-weight:700;
    font-size:38px;line-height:1.08;letter-spacing:-.02em;margin:0 0 10px}
  .standfirst{color:#666E77;margin:0;max-width:64ch}
  table.totals{width:100%;border-collapse:collapse;margin-top:30px;table-layout:fixed}
  table.totals td{vertical-align:top;padding:0 18px 0 0;border:none}
  .total-figure{font-family:"Archivo","Helvetica Neue",Arial,sans-serif;font-weight:700;
    font-size:32px;letter-spacing:-.03em;line-height:1.05}
  .total-label{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:10.5px;
    letter-spacing:.08em;text-transform:uppercase;color:#666E77;margin-top:8px;line-height:1.45}
  .up{color:#9E3A26}.down{color:#1F5F4E}.flat{color:#767C84}
  section{margin-top:48px}
  h2{font-family:"Archivo","Helvetica Neue",Arial,sans-serif;font-weight:600;font-size:13px;
    letter-spacing:.12em;text-transform:uppercase;margin:0;padding-bottom:10px;
    border-bottom:1px solid #12161C}
  .sub{color:#666E77;font-size:14px;margin:13px 0 20px;max-width:68ch}
  table.spine{width:100%;border-collapse:collapse;border-left:2px solid #12161C}
  table.spine td{border-bottom:1px solid #DCDED8;padding:13px 10px;vertical-align:middle}
  table.spine tr:last-child td{border-bottom:none}
  .nm{padding-left:18px;font-weight:600;font-size:14px;width:220px}
  .nm em{display:block;font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:11px;
    color:#666E77;font-weight:400;font-style:normal;margin-top:3px}
  .bar{display:block;height:19px;line-height:19px;font-size:1px;overflow:hidden;background:#12161C}
  .bar.hot{background:#9E3A26}
  .cost{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:14px;font-weight:500;
    text-align:right;white-space:nowrap;width:104px}
  .dols,.pct{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:12.5px;
    font-weight:500;text-align:right;white-space:nowrap;width:84px}
  tr.flag td{background:#FAF3F0}
  tr.flag td.nm{border-left:3px solid #9E3A26;padding-left:15px}
  .attrib{margin-top:28px;border-top:1px solid #DCDED8;padding-top:22px}
  .attrib-title{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:11px;
    letter-spacing:.1em;text-transform:uppercase;color:#666E77;margin:0 0 12px}
  table.stack{width:100%;border-collapse:collapse;table-layout:fixed}
  table.stack td{height:34px;padding:0;border:none}
  table.keys{width:100%;border-collapse:collapse;margin-top:14px}
  table.keys td{padding:0 12px 6px 0;font-size:12.5px;color:#666E77;vertical-align:middle;border:none}
  .sw{display:inline-block;width:11px;height:11px;margin-right:7px;vertical-align:-1px;font-size:1px}
  .attrib-note{color:#666E77;font-size:13.5px;margin:12px 0 0;max-width:68ch}
  table.res-table{width:100%;border-collapse:collapse;margin-top:4px}
  table.res-table th{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:10.5px;
    letter-spacing:.09em;text-transform:uppercase;color:#666E77;text-align:left;font-weight:500;
    padding:0 10px 9px 0;border-bottom:1px solid #12161C}
  table.res-table th.num,table.res-table td.num{text-align:right;padding-right:0}
  table.res-table td{padding:11px 10px 11px 0;border-bottom:1px solid #DCDED8;
    vertical-align:top;font-size:13.5px}
  tr.row{cursor:pointer}
  tr.row:hover td{background:#F4F4F0}
  .tw{display:inline-block;width:14px;color:#9AA0A6;font-size:11px}
  tr.row.open .tw{color:#12161C}
  .res{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:13px;font-weight:500}
  .res em{display:block;font-family:"Public Sans","Helvetica Neue",Arial,sans-serif;font-size:12px;
    color:#666E77;font-weight:400;font-style:normal;margin-top:3px;padding-left:14px}
  .cloud{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:10.5px;
    letter-spacing:.06em;border:1px solid #DCDED8;padding:1.5px 6px;color:#666E77;white-space:nowrap}
  table.res-table td.num{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;
    font-size:13.5px;white-space:nowrap}
  td.save{color:#1F5F4E;font-weight:600}
  tr.detail{display:none}
  tr.detail.open{display:table-row}
  tr.detail td{background:#F2F2EE;padding:0;border-bottom:1px solid #12161C}
  .panel{padding:22px 26px 24px}
  .panel-head{margin-bottom:12px}
  .savepill{display:inline-block;background:#1F5F4E;color:#fff;font-weight:600;font-size:12.5px;
    padding:3px 10px;margin-right:10px}
  .savepill.zero{background:#767C84}
  .conf{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:11px;
    letter-spacing:.06em;text-transform:uppercase;color:#666E77}
  .finding{margin:0 0 18px;max-width:96ch;font-size:14px}
  .cols{width:100%;margin-bottom:6px}
  .cols:after{content:"";display:table;clear:both}
  .col{float:left;width:48%;margin-right:4%}
  .col:last-child{margin-right:0}
  .codewrap{margin-bottom:16px}
  .codelabel{font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:10.5px;
    letter-spacing:.08em;text-transform:uppercase;color:#666E77;margin-bottom:6px}
  pre.code{margin:0;background:#12161C;color:#E8E8E4;padding:14px 16px;overflow-x:auto;
    font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:11.5px;line-height:1.6}
  pre.code code{font-family:inherit}
  .nochange{font-size:13.5px;color:#666E77;font-style:italic;margin:22px 0 0}
  .panel h4{font-family:"Archivo","Helvetica Neue",Arial,sans-serif;font-size:11px;
    letter-spacing:.09em;text-transform:uppercase;color:#12161C;margin:0 0 8px}
  ul.maths{margin:0 0 16px;padding-left:18px;font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;
    font-size:11.5px;color:#3C4550;line-height:1.75}
  .risk{font-size:13px;color:#3C4550;margin:0 0 16px}
  footer{margin-top:52px;padding-top:16px;border-top:1px solid #DCDED8;
    font-family:"IBM Plex Mono","SF Mono",Consolas,monospace;font-size:11px;color:#666E77;line-height:1.7}
  @media (max-width:820px){
    .wrap{padding:0 18px 56px}
    h1{font-size:27px}.total-figure{font-size:24px}
    td.barcell,th.opt,td.opt{display:none}
    .nm{padding-left:14px;width:auto}
    .col{float:none;width:100%;margin-right:0}
  }
  @media print{
    body{background:#fff}.wrap{max-width:100%;padding:0}
    tr.detail{display:table-row}
    section{page-break-inside:avoid}
  }
"""

FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700'
         '&family=Public+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap"'
         ' rel="stylesheet">')

# The templates below are filled with %-formatting, so every literal percent sign
# inside the stylesheet has to be doubled or the format call fails.
STYLE_LITERAL = STYLE.replace("%", "%%")

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloud cost savings report &mdash; %(period)s</title>
""" + FONTS + """
<style>""" + STYLE_LITERAL + """</style>
</head>
<body>
<div class="wrap">

<header>
  <p class="eyebrow">Sample report &middot; mock data &middot; not real billing</p>
  <h1>Cloud cost savings report</h1>
  <p class="standfirst">Azure and GCP, billing month of %(period)s, split by category and by owning
  team. Click any resource to see the configuration driving its cost and the change that would
  reduce it.</p>

  <table class="totals">
    <tr>
      <td>
        <div class="total-figure">%(total)s</div>
        <div class="total-label">Invoiced this month</div>
      </td>
      <td>
        <div class="total-figure %(increase_tone)s">%(increase_pct)s</div>
        <div class="total-label">Against last month</div>
      </td>
      <td>
        <div class="total-figure">%(riser_share)s</div>
        <div class="total-label">Of the rise is %(riser_name)s</div>
      </td>
      <td>
        <div class="total-figure down">%(saving)s</div>
        <div class="total-label">Identified saving per month</div>
      </td>
      <td>
        <div class="total-figure down">%(saving_year)s</div>
        <div class="total-label">If held for a year</div>
      </td>
    </tr>
  </table>
</header>

<section>
  <h2>Where the money went</h2>
  <p class="sub">Six categories, ordered by spend. Bars share one scale, so the length of each is
  its size relative to the largest.</p>
  <table class="spine">%(cat_rows)s
  </table>
</section>

<section>
  <h2>Spend by engineering team</h2>
  <p class="sub">Assigned by resource owner tag. The dollar column is what changed since last month,
  and it matters more than the percentage: a large team moving a few per cent costs more than a
  small team moving twenty.</p>
  <table class="spine">%(team_rows)s
  </table>

  <div class="attrib">
    <p class="attrib-title">Who caused the %(increase_money)s increase</p>
    <table class="stack"><tr>%(stack_cells)s</tr></table>
    <table class="keys"><tr>%(stack_keys)s</tr></table>
    <p class="attrib-note">Teams whose spend fell are netted off the total rather than shown here.</p>
  </div>
</section>

<section>
  <h2>Top resources &mdash; click any row to open it</h2>
  <p class="sub">These account for %(top_sum)s, roughly %(top_share)s of the month. Shaded rows rose
  more than 40%% and have not been explained yet. Opening a row shows the current configuration, the
  change that would reduce the cost, how the saving was calculated, and what could go wrong.</p>

  <table class="res-table">
    <thead>
      <tr>
        <th width="30%%">Resource</th>
        <th class="opt" width="8%%">Cloud</th>
        <th class="opt">Team</th>
        <th class="num" width="11%%">Cost</th>
        <th class="num" width="10%%">vs last</th>
        <th class="num" width="12%%">Saving</th>
      </tr>
    </thead>
    <tbody>%(res_rows)s
    </tbody>
  </table>
</section>

<section>
  <h2>Where the savings are</h2>
  <p class="sub">%(counted)s Ordered by money on the table, not by how easy each one is.</p>
  <table class="spine">%(action_rows)s
  </table>
  <p class="attrib-note">Total identified: %(saving)s a month, %(saving_pct)s of the invoice,
  %(saving_year)s over a year if the changes hold.</p>
</section>

<footer>
  Generated %(generated)s by generate_report.py &middot; All team names, resource names, figures and
  configurations are fictional<br>
  Every figure on this page is derived from the billing file; nothing is hard-coded in the template
</footer>

</div>
<script>
(function () {
  var rows = document.querySelectorAll('tr.row');
  for (var i = 0; i < rows.length; i++) {
    (function (row) {
      row.addEventListener('click', function () {
        var panel = row.nextElementSibling;
        if (!panel || panel.className.indexOf('detail') === -1) { return; }
        var isOpen = panel.className.indexOf('open') !== -1;
        panel.className = isOpen ? 'detail' : 'detail open';
        row.className = isOpen ? row.className.replace(' open', '')
                               : row.className + ' open';
      });
    })(rows[i]);
  }
})();
</script>
</body>
</html>
"""

FAIL_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cost report FAILED</title>
""" + FONTS + """
<style>""" + STYLE_LITERAL + """
  .failbar{background:#9E3A26;color:#fff;padding:22px 26px;margin-bottom:26px}
  .failbar h1{color:#fff;font-size:30px;margin:0}
  .failbar p{margin:8px 0 0;color:#F6DFDA}
  ul.why{padding-left:20px}
  ul.why li{margin-bottom:9px}
</style>
</head>
<body>
<div class="wrap">
  <div class="failbar">
    <h1>RUN FAILED</h1>
    <p>No report was produced for this period. This file exists so that a failed run
    cannot be mistaken for a month nobody looked at.</p>
  </div>
  <h2>What went wrong</h2>
  <ul class="why">%(items)s</ul>
  <p>Fix the billing file and run the script again. Nothing was published, because publishing
  numbers that do not reconcile is worse than publishing nothing.</p>
  <footer>
    Attempted %(generated)s &middot; Last successful run: %(last_good)s
  </footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def read_last_good(marker):
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="Build a cloud cost savings report.")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="billing JSON file")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="HTML file to write")
    args = ap.parse_args()

    data_path = pathlib.Path(args.data)
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    marker = out_path.parent / ".last-good-run"

    stamp = dt.datetime.now().strftime("%d %B %Y at %H:%M")
    last_good = read_last_good(marker)

    print("cloud-cost-report -- %s" % stamp)

    try:
        print("  Step 1: reading %s" % data_path)
        data = load(data_path)

        print("  Step 2: checking the numbers add up...")
        problems = check(data)
        if problems:
            raise DataProblem("; ".join(problems))
        print("  totals reconcile: categories, teams and invoice all agree.")

        print("  Step 3: working out where the savings are...")
        s = summarise(data)
        print("  %d resources, %d with an identified saving, %s a month total."
              % (len(s["resources"]), len(s["actionable"]), money(s["saving"])))

        print("  Step 4: writing the report...")
        out_path.write_text(render(s, stamp), encoding="utf-8")
        marker.write_text(stamp, encoding="utf-8")

        print("")
        print("Done. Wrote %s" % out_path.resolve())
        return 0

    except Exception as exc:                      # noqa: BLE001 - deliberate catch-all
        reasons = [str(exc)] if isinstance(exc, DataProblem) else [
            "Unexpected error: %s" % exc,
            "See the traceback printed to the terminal.",
        ]
        try:
            out_path.write_text(render_failure(reasons, stamp, last_good), encoding="utf-8")
            print("")
            print("RUN FAILED. Wrote a failure report to %s" % out_path.resolve())
        except OSError as write_err:
            print("RUN FAILED and the failure report could not be written: %s" % write_err)
        for line in reasons:
            print("  - %s" % line)
        if not isinstance(exc, DataProblem):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
