# cloud-cost-report

Turns a monthly cloud billing export into a one-page HTML report that says where the
money went, which team caused the increase, and — for each resource — the exact
configuration change that would reduce it.

Built as a companion to [`weekly-brief`](https://github.com/ranjitkumarkonjeti/weekly-brief).
Same two safety rules, applied to a different problem.

## What it does

1. Reads a billing file (`data/august-2026.json`).
2. **Checks the numbers add up.** Categories, teams and the invoiced total must all
   agree. If they don't, the run fails and nothing is published.
3. Works out each team's share of the month and of the *increase*, which is not the
   same thing — the biggest spender is usually not the biggest mover.
4. Totals the identified savings and ranks them by money on the table.
5. Writes one self-contained HTML file. No build step, no server, no dependencies.

## The two rules

**It always writes a file.** If the data is missing, malformed, or doesn't reconcile,
it writes a report with `RUN FAILED` at the top explaining exactly what broke, and
exits non-zero. It never finishes silently, because a missing report looks identical
to a month nobody looked at.

**It refuses to publish numbers that don't reconcile.** A cost report that is subtly
wrong is worse than no cost report, because someone will act on it. If the categories
and the teams disagree by more than a dollar, the run stops.

Both rules came from the Failure Mode Map in my IMPACT Living Document, Section 4.

## Running it

Python 3.8 or newer. No packages to install — standard library only.

```bash
python generate_report.py
```

Then open `output/cloud-cost-report.html` in any browser.

Options:

```bash
python generate_report.py --data data/august-2026.json --out output/report.html
```

## What a run looks like

```
cloud-cost-report -- 23 August 2026 at 04:24
  Step 1: reading data/august-2026.json
  Step 2: checking the numbers add up...
  totals reconcile: categories, teams and invoice all agree.
  Step 3: working out where the savings are...
  10 resources, 9 with an identified saving, $29,690 a month total.
  Step 4: writing the report...

Done. Wrote output/cloud-cost-report.html
```

And when the data is wrong:

```
  Step 2: checking the numbers add up...

RUN FAILED. Wrote a failure report to output/cloud-cost-report.html
  - Teams add up to $153,320 but the invoiced total is $148,320 (out by $5,000).
```

## The drill-down

Clicking a resource row in the report opens a panel with five things:

| Part | What it answers |
|---|---|
| Finding | Why this resource costs what it does |
| Current configuration | The actual Terraform, SQL, JSON or Python driving the cost |
| The change | The specific edit that reduces it |
| How the number is worked out | Every step of the arithmetic, so the figure can be argued with |
| What could go wrong, and effort | The risk of making the change, and how long it takes |

The arithmetic is shown on purpose. A savings figure nobody can check is a figure
nobody will act on.

## The data file

```jsonc
{
  "period": "August 2026",
  "invoiced_total": 148320,
  "prior_total": 135560,
  "categories": [ { "name": "Compute", "cost": 58940, "prior": 54472 } ],
  "teams":      [ { "name": "Core API", "cost": 46180, "prior": 43785,
                    "top_resource": "aks-prod-eastus-nodepool3" } ],
  "resources":  [ {
    "id": "vm-scaleset-batch-nonprod",
    "cloud": "AZURE", "category": "Compute", "team": "Developer Experience",
    "cost": 5940, "prior": 4032,
    "flag": true,                    // shade the row, needs explaining
    "saving": 3800,
    "confidence": "high",
    "finding": "...",                // why it costs what it does
    "current_config": { "lang": "hcl", "label": "...", "code": "..." },
    "change":         { "lang": "hcl", "label": "...", "code": "..." },
    "maths": [ "...", "..." ],       // every step, shown in the report
    "risk": "...",
    "effort": "..."
  } ]
}
```

A resource with `"saving": 0` and `"change": null` renders as "No saving identified".
That case is deliberate and should stay in the data: a report that finds something
wrong with every single resource is not being honest.

## Honest limitations

- **The data is mock.** Real figures come from an Azure Cost Management export and a
  GCP billing export to BigQuery. Wiring those up needs billing-reader credentials I
  don't have, which is precisely why this stayed a report generator instead of
  becoming a live tool.
- **Savings are estimates, not quotes.** Discount rates move. Every figure shows its
  arithmetic so it can be rechecked against current rates rather than trusted.
- **Nothing is applied automatically.** The tool writes a file. Any change is made by
  a person who has read the risk line first. That is the same choice as `weekly-brief`,
  for the same reason: the cost of a wrong action here is real.

## Layout

```
cloud-cost-report/
├── generate_report.py       the whole tool, one file
├── data/
│   └── august-2026.json     the billing export
├── output/
│   ├── cloud-cost-report.html
│   └── .last-good-run       date of the last successful run
└── README.md
```
