# mega-usage-report

Turn a MEGA **"Export - User Activity as CSV"** file into a customer-ready New Relic usage report: who uses which product areas, how often, and whether their user type matches their activity.

One command produces:

- an **HTML report** with charts, a heatmap and a user list, which saves to a compact PDF;
- a **lookup-table CSV** to upload to New Relic;
- a **dashboard JSON** that turns that table into a live New Relic dashboard;
- a renamed copy of the **raw export** to send alongside the PDF.

It uses only the Python standard library (Python 3.8 or newer), so there's nothing to install.

## Contents

- [Quick start](#quick-start)
- [What you get](#what-you-get)
- [Using the HTML report](#using-the-html-report)
- [Command-line options](#command-line-options)
- [Monthly routine](#monthly-routine)
- [Setting up the New Relic dashboard](#setting-up-the-new-relic-dashboard)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Customer data and privacy](#customer-data-and-privacy)
- [Development](#development)

## Quick start

```bash
git clone <this repo>
cd mega-usage-report

# Try it on the made-up sample first
python3 mega_report.py sample/sample_export.csv --customer "Acme" --account-id 1234567

# Then on a real export
python3 mega_report.py "Export---User-Activity-as-CSV.csv" --customer "Acme" --account-id <NR account ID>
```

The output goes to `output/<Customer>_<YYYY-MM>/`, where the month comes from the latest "Last Active" date in the export. The script also prints a short summary:

```
Acme: 40 users (22 FULL, 17 BASIC, 1 CORE)
Activity from Oct 7, 2026 to Nov 5, 2026
Most-used features: Data Explorer - UQE (82%), Alerts (70%), Dashboards (70%), Logs (48%), APM (42%)
```

## What you get

| File | What it's for |
|---|---|
| `<Customer>_NewRelic_Usage_<YYYY-MM>.html` | The report. Open it in any browser. Its **Save as PDF** button makes the PDF you send the customer. |
| `<Customer>_user_activity_<YYYY-MM>.csv` | The raw MEGA export, unchanged, renamed for sending. |
| `<Customer>UserFeatureUsage_<YYYY-MM>.csv` | The New Relic lookup table, one row per person per feature. |
| `<Customer>_Usage_Dashboard.json` | The New Relic dashboard, which reads the lookup table. Without `--account-id`, it's written as `..._NEEDS_ACCOUNT_ID.json` with placeholder account `12345`. |

To see real output, look in `examples/Acme_2026-11/`, which was built from the 40-person made-up sample.

## Using the HTML report

The report is a single, self-contained HTML file. The data is stored inside it, and nothing is uploaded anywhere. The only network request is for the web font, and the report falls back to Helvetica or Arial when offline.

### Sections

| Section | Shows |
|---|---|
| **Summary** | Headline, period, users by type, page views, average days active, features per person |
| **Who uses what** | Heatmap of people × features (top 25, 50 or 100 users, everyone or FULL or BASIC only) |
| **Feature adoption** | People per feature split by user type, user-type donut, days-active histogram, most-used accounts |
| **User type vs. activity** | FULL users with light activity and BASIC users with heavy activity. You can change both thresholds. |
| **Every user** | One card per person with every feature and its page views. You can search, filter by type or feature, and sort. |

### Buttons

| Button | What it does |
|---|---|
| **Load this month's CSV** | Replaces the data with a new MEGA export and redraws everything (in the browser only) |
| **Compare with last month** | Loads last month's export. Every number and feature bar then shows its change, plus new and dropped users. |
| **Save as PDF** | Lets you pick sections and how many users to include, then opens the print window. Each section starts on a new page. The user list prints as a compact table. |
| **Export for New Relic** | Downloads the lookup-table CSV. It's identical to what the script writes. |
| **Save report file** | Downloads a new copy of the report with the currently loaded data in it |

The report sets a zero page margin when printing, so the browser leaves out its own header and footer (date, title, file path, page number). If they still appear, open **More settings** in the print window and untick **Headers and footers**.

The PDF window shows a page estimate. With 30 users in the list it's about 7 pages, and with all users it's about 1 page per 28 people.

## Command-line options

```
python3 mega_report.py [export.csv] --customer NAME [options]
```

| Option | Default | Description |
|---|---|---|
| `export` | required | MEGA user activity CSV. Not needed with `--dashboard-only`. |
| `--customer` | required | Name shown in the report. Also used to build file and table names. |
| `--previous` | none | Last month's export, to show changes in the report |
| `--account-id` | placeholder `12345` | New Relic account the lookup table is uploaded to |
| `--dashboard-only` | | Rebuild just the dashboard JSON. Needs `--customer` and `--account-id`, but no CSV. |
| `--out` | `output` | Output folder |
| `--table-name` | `<Customer>UserFeatureUsage` | Lookup table name. Use this to match an existing table exactly; names are case-sensitive. |
| `--full-light-days` | `2` | Report and dashboard: FULL users active for this many days or less (1–31) |
| `--basic-heavy-days` | `10` | Report and dashboard: BASIC users active for this many days or more (1–31) |

### Examples

```bash
# Month-over-month report
python3 mega_report.py november.csv --customer "Acme" --previous october.csv

# Point the dashboard at a table that already exists with a different capitalisation
python3 mega_report.py export.csv --customer "acme" --table-name acmeUserFeatureUsage --account-id 1234567

# Account ID found later: rebuild only the dashboard (and delete the placeholder file)
python3 mega_report.py --dashboard-only --customer "Acme" --account-id 1234567 --out output/Acme_2026-11
```

## Monthly routine

1. Export the customer's user activity CSV from MEGA.
2. Run the script, with `--previous` pointing at last month's export.
3. Open the HTML report, click **Save as PDF** and choose the sections.
4. Send the PDF and the `_user_activity_` CSV.
5. If the customer uses the dashboard, **update** the existing lookup table with the new `UserFeatureUsage` CSV. The dashboard picks it up automatically.

Keep each month's export. It's needed for `--previous`, and later for history and trends.

## Setting up the New Relic dashboard

Do this once per customer.

1. **Pick the account.** A lookup table can only be queried from the account it was uploaded to. Agree with the customer which account that is.
2. **Upload the table.** In New Relic, go to **Logs → Lookup tables → upload**. Choose the `UserFeatureUsage` CSV and name the table exactly what the script printed, for example `AcmeUserFeatureUsage`. The name can't be changed later.
3. **Check the table**, with the query the script printed:
   ```sql
   SELECT count(*), uniqueCount(email) FROM lookup(AcmeUserFeatureUsage)
   ```
4. **Import the dashboard.** Go to **Dashboards → Import dashboard**, paste the JSON and import it.

### Dashboard pages

| Page | Widgets |
|---|---|
| **Overview** | Report period, users by type, people per feature, user-type pie, days-active buckets, most-used accounts, adoption by user type |
| **People** | **Every user and what they use**: one row per person, with the feature list in one column. **Page views per person and feature**: one row per feature, full detail. |
| **User type vs. activity** | FULL users active ≤ 2 days, BASIC users active ≥ 10 days |

### New Relic limits

| Limit | Value | How the script handles it |
|---|---|---|
| Rows per table | 20,000 | Warns if over |
| File size | 4 MB | Warns if over |
| Length of each value | 255 bytes | Caps `featuresUsed` and stops if anything else is too long |
| Tables per account | 250 | |
| Who can upload | FULL or CORE users | |

Uploading a table updates it all at once: you can't add rows to an existing table. Lookup tables are kept until you delete them.

## How it works

```
MEGA export (one row per person, one column per feature)
        │
        ▼
mega_report.py ── read_export() ──► list of users + feature columns
        │
        ├─► render_html()      → embeds the raw CSV + customer name into templates/report_template.html
        ├─► write_lookup_csv() → reshapes to one row per person per feature (+ featuresUsed)
        ├─► build_dashboard()  → dashboard JSON whose NRQL reads lookup(<table>)
        └─► copy of the raw export
```

### Reading the export

Seven columns describe the person: `Email`, `Total Page Views`, `NRQLs Executed`, `Latest Account`, `Last Active`, `User Type` and `Days Active`. **Every other column is treated as a feature**, so new MEGA feature columns are picked up automatically. `Email`, `User Type`, `Days Active` and `Total Page Views` are required. Blank or non-numeric values count as 0, and display names are built from the email (`Jane_Doe@example.com` becomes "Jane Doe").

### The HTML report

`templates/report_template.html` is a static page with all of its CSS and JavaScript inline. The script fills in three placeholders: the customer name, a small JSON config (customer, file prefix, table name), and the raw CSV text, inside a `<script type="text/plain">` block. When the page opens, its JavaScript parses that CSV and draws every chart as plain HTML/SVG, with no chart library. That's why the report's own buttons can load a different CSV and save a new copy without the script.

### The lookup-table CSV

New Relic charts need **one row per person per feature**, so the export is reshaped:

| Column | Meaning |
|---|---|
| `reportMonth`, `periodStart`, `periodEnd` | Which export this came from |
| `email`, `userName`, `userType` | The person |
| `feature`, `pageViews` | One feature and that person's page views in it |
| `daysActive`, `totalPageViews`, `nrqlExecuted`, `lastActive`, `latestAccount` | Person-level values, repeated on each of their rows |
| `featuresUsed` | That person's full feature list, busiest first, e.g. `Dashboards (398), Alerts (83)`, repeated on each of their rows |

Rules:

- Features with 0 page views are left out.
- People with no feature activity get one row with feature `(none)`, so they still count.
- `featuresUsed` exists so a dashboard table can show **one row per person**. Using it as a facet groups the rows without needing NRQL to concatenate text.
- New Relic rejects any value over 255 bytes. A long `featuresUsed` keeps the busiest features and ends with `+N more`. The per-feature rows, the HTML report and the PDF always show the full list.

### The dashboard

Each widget is NRQL against `lookup(<table>)`:

- **`max()`, not `latest()`.** Lookup rows have no timestamp, and the person-level values are identical on every row of a person.
- **No `ORDER BY … DESC`.** In a FACET query, NRQL rejects `ASC`/`DESC`. Facets are already chosen and sorted from highest to lowest by the first SELECT field.
- **`email` is in the facet** next to `userName`, so two people with the same display name aren't merged.
- **Time range is ignored** (`ignoreTimeRange`). Lookup data isn't tied to a time period.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Upload fails: "Attribute values cannot exceed 255 bytes" | A value over 255 bytes. Older versions of this script didn't cap `featuresUsed`. | Re-run with the current script |
| Dashboard import fails: "Failed to create dashboard" | New Relic couldn't accept one of the queries | In the browser console, find "Error parsing …" to see which query, then run it in the query builder |
| `NRQL Syntax Error … unexpected 'DESC'` | `ORDER BY … DESC` in a FACET query | Remove the `ORDER BY`. Facets are already sorted from highest to lowest. |
| Widgets: "No data found for external data source: lookup(…)" | The table isn't in that account, the name's capitalisation doesn't match, or the upload hasn't finished | Check **Logs → Lookup tables** in that account, run `SELECT count(*) FROM lookup(<table>)`, and wait a few minutes after uploading |
| A new column (e.g. `featuresUsed`) comes back blank | New Relic still has the old table; a missing column comes back blank instead of raising an error | **Update** the existing table, keep the same name, and wait a few minutes |
| Every widget errors after import | The dashboard still uses placeholder account `12345` | Rebuild it with `--dashboard-only --account-id <ID>` |
| PDF is very long | All users were printed | In the PDF window, choose fewer users, or send the CSV for the full list |
| PDF shows a date and file path on each page | The browser's headers and footers | In the print window, open **More settings** and untick **Headers and footers** |

## Customer data and privacy

MEGA exports contain names, emails and activity.

- **Never commit real exports or outputs.** `.gitignore` excludes `output/`, `*.csv` and `*.pdf`, except the made-up `sample/` and `examples/` files.
- **Lookup-table data isn't masked in New Relic.** Anyone in the account can query it, so agree with the customer which account it goes in.
- **The HTML report contains the full data.** Anyone with the file can read every email in it, so share it like the CSV itself.

## Development

```bash
python3 -m unittest discover tests
```

```
mega_report.py                 the CLI and all logic
templates/report_template.html the HTML report (CSS + JS inline)
tests/test_mega_report.py      unit and end-to-end tests
sample/sample_export.csv       40 made-up users, used by the tests
examples/Acme_2026-11/         output generated from the sample
```

The lookup CSV logic exists twice, in Python (`lookup_rows`) and in the report's **Export for New Relic** button. If you change one, change the other, and check that both produce the same file for the same export.

After changing the template or script, regenerate the example:

```bash
rm -rf examples && python3 mega_report.py sample/sample_export.csv --customer Acme --account-id 1000001 --out examples
```

### Ideas not built yet

- **History table:** a second lookup table holding every month (keyed on `reportMonth`), re-uploaded in full each month, plus a Trends page.
- **Customer copy of the HTML:** a version without the Load/Compare/Save buttons and without the User type vs. activity section.
