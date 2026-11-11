#!/usr/bin/env python3
"""Turn a MEGA user-activity export into a customer usage report.

Given the raw clickstream CSV that MEGA exports for one customer, this writes:

  * an HTML report (charts, heatmap, user list, PDF export) with the data embedded
  * a lookup-table CSV for New Relic (one row per person per feature)
  * a New Relic dashboard JSON that reads that lookup table
  * a copy of the raw export, named for sending to the customer

Uses only the Python standard library (3.8+).

Example:
    python3 mega_report.py export.csv --customer "Acme" --account-id 1234567
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "templates" / "report_template.html"

# Columns that describe the person. Every other column is treated as a feature.
META_COLUMNS = [
    "Email", "Total Page Views", "NRQLs Executed", "Latest Account",
    "Last Active", "User Type", "Days Active",
]
REQUIRED_COLUMNS = ["Email", "User Type", "Days Active", "Total Page Views"]

LOOKUP_COLUMNS = [
    "reportMonth", "periodStart", "periodEnd", "email", "userName", "userType",
    "feature", "pageViews", "daysActive", "totalPageViews", "nrqlExecuted",
    "lastActive", "latestAccount", "featuresUsed",
]
NO_FEATURE = "(none)"
NO_FEATURE_TEXT = "No feature activity"
# Used in the dashboard JSON when no --account-id is given. Imports, but every widget
# errors until it is replaced, so the problem is obvious rather than silent.
PLACEHOLDER_ACCOUNT_ID = 12345

# New Relic lookup-table limits.
LOOKUP_MAX_ROWS = 20_000
LOOKUP_MAX_BYTES = 4 * 1024 * 1024
LOOKUP_MAX_VALUE_BYTES = 255  # per cell; longer values make the whole upload fail

DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y"]


class ExportError(ValueError):
    """The input file isn't a usable MEGA export."""


@dataclass
class User:
    email: str
    name: str
    user_type: str
    views: float
    nrql: float
    days: float
    account: str
    last_active: Optional[date]
    features: Dict[str, float] = field(default_factory=dict)  # only features with views > 0


@dataclass
class Export:
    path: Path
    raw_text: str
    feature_columns: List[str]
    users: List[User]

    @property
    def period_start(self) -> Optional[date]:
        dates = [u.last_active for u in self.users if u.last_active]
        return min(dates) if dates else None

    @property
    def period_end(self) -> Optional[date]:
        dates = [u.last_active for u in self.users if u.last_active]
        return max(dates) if dates else None

    @property
    def report_month(self) -> str:
        end = self.period_end or date.today()
        return f"{end.year}-{end.month:02d}"


# ---------------------------------------------------------------- parsing

def to_number(value: str) -> float:
    """Mirror the report's JavaScript (`+value || 0`): blanks and junk become 0."""
    try:
        n = float((value or "").strip())
    except ValueError:
        return 0
    if n != n:  # NaN
        return 0
    return int(n) if n.is_integer() else n


def parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def name_from_email(email: str) -> str:
    """Jane_Doe@example.com -> Jane Doe (same rule the report uses)."""
    local = email.split("@")[0]
    parts = [p for p in re.split(r"[._]+", local) if p]
    return " ".join(p[0].upper() + p[1:].lower() for p in parts)


def read_export(path: Path) -> Export:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")
    rows = list(csv.reader(io.StringIO(raw)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise ExportError(f"{path.name} is empty.")

    header = [h.strip() for h in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ExportError(
            f"{path.name} is missing column(s): {', '.join(missing)}. "
            "Export the user activity CSV from MEGA and try again."
        )
    features = [h for h in header if h and h not in META_COLUMNS]
    if not features:
        raise ExportError(f"{path.name} has no feature columns (Alerts, APM, Logs ...).")

    users: List[User] = []
    seen = set()
    for line_no, row in enumerate(rows[1:], start=2):
        rec = dict(zip(header, row + [""] * (len(header) - len(row))))
        email = rec.get("Email", "").strip()
        if not email:
            continue
        if email.lower() in seen:
            print(f"  warning: duplicate email on line {line_no} ({email}); keeping both rows", file=sys.stderr)
        seen.add(email.lower())
        users.append(User(
            email=email,
            name=name_from_email(email),
            user_type=(rec.get("User Type", "").strip().upper() or "UNKNOWN"),
            views=to_number(rec.get("Total Page Views", "")),
            nrql=to_number(rec.get("NRQLs Executed", "")),
            days=to_number(rec.get("Days Active", "")),
            account=rec.get("Latest Account", "").strip(),
            last_active=parse_date(rec.get("Last Active", "")),
            features={f: v for f in features if (v := to_number(rec.get(f, ""))) > 0},
        ))
    if not users:
        raise ExportError(f"{path.name} has a header but no user rows.")
    return Export(path=path, raw_text=raw, feature_columns=features, users=users)


# ---------------------------------------------------------------- outputs

def safe_name(customer: str) -> str:
    """'Acme Inc.' -> 'Acme_Inc' for file names."""
    return re.sub(r"[^A-Za-z0-9]+", "_", customer).strip("_") or "Customer"


def table_name(customer: str) -> str:
    """'Acme Inc.' -> 'AcmeIncUserFeatureUsage' (valid New Relic table name)."""
    base = re.sub(r"[^A-Za-z0-9]", "", customer) or "Customer"
    if base[0].isdigit():
        base = "C" + base
    return f"{base}UserFeatureUsage"


def lookup_rows(export: Export) -> List[list]:
    month = export.report_month
    start = export.period_start.isoformat() if export.period_start else ""
    end = export.period_end.isoformat() if export.period_end else ""
    out = []
    for u in export.users:
        summary = features_used(u)
        feats = sorted(u.features.items(), key=lambda kv: -kv[1]) or [(NO_FEATURE, 0)]
        for feature, views in feats:
            out.append([
                month, start, end, u.email, u.name, u.user_type, feature, views,
                u.days, u.views, u.nrql,
                u.last_active.isoformat() if u.last_active else "", u.account, summary,
            ])
    return out


def features_used(user: User) -> str:
    """One person's features as text, busiest first, e.g. 'Dashboards (398), Alerts (83)'.

    Repeated on each of the person's lookup rows so a dashboard table can show one row per
    person (FACET ... featuresUsed). Thousands separators are left out because the list
    itself is comma-separated.

    New Relic rejects any lookup value over 255 bytes, so a long list keeps the busiest
    features and ends with "+N more". The full detail is still in the per-feature rows.
    """
    if not user.features:
        return NO_FEATURE_TEXT
    items = sorted(user.features.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    parts = [f"{name} ({int(v) if float(v).is_integer() else v})" for name, v in items]
    return fit_list(parts, LOOKUP_MAX_VALUE_BYTES)


def fit_list(parts: List[str], limit: int) -> str:
    """Join parts with ', ', dropping from the end and adding '+N more' to stay within limit bytes."""
    full = ", ".join(parts)
    if len(full.encode("utf-8")) <= limit:
        return full
    for keep in range(len(parts) - 1, -1, -1):
        text = ", ".join(parts[:keep] + [f"+{len(parts) - keep} more"])
        if len(text.encode("utf-8")) <= limit:
            return text
    return f"+{len(parts)} more"


def check_value_lengths(rows: List[list]) -> None:
    """Stop before writing a file New Relic would reject (any value over 255 bytes)."""
    for n, row in enumerate(rows, start=2):
        for col, value in zip(LOOKUP_COLUMNS, row):
            size = len(str(value).encode("utf-8"))
            if size > LOOKUP_MAX_VALUE_BYTES:
                raise ExportError(
                    f"lookup CSV line {n}, column {col} is {size} bytes; New Relic's limit is "
                    f"{LOOKUP_MAX_VALUE_BYTES}. Value starts: {str(value)[:60]!r}")


def write_lookup_csv(export: Export, path: Path) -> int:
    rows = lookup_rows(export)
    check_value_lengths(rows)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(LOOKUP_COLUMNS)
        w.writerows(rows)
    return len(rows)


def render_html(export: Export, customer: str, previous: Optional[Export] = None,
                template_path: Path = TEMPLATE_PATH, table: Optional[str] = None,
                full_light_days: int = 2, basic_heavy_days: int = 10) -> str:
    template = template_path.read_text(encoding="utf-8")
    config = {"customer": customer, "slug": safe_name(customer), "table": table or table_name(customer),
              "fullLightDays": full_light_days, "basicHeavyDays": basic_heavy_days}

    def embed(text: str) -> str:
        return text.replace("</script", "<\\/script")

    config_json = json.dumps(config).replace("</", "<\\/")
    out = template.replace("__CUSTOMER_HTML__", html.escape(customer))
    # Order matters: replace the data blocks last so CSV text is never searched for placeholders.
    out = out.replace("__CONFIG__", config_json, 1)
    out = out.replace("__CSV_PREVIOUS__", embed(previous.raw_text) if previous else "__CSV_PREVIOUS__", 1)
    out = out.replace("__CSV_CURRENT__", embed(export.raw_text), 1)
    return out


def _widget(title, viz, query, col, row, width, height, account_id):
    cfg = {"nrqlQueries": [{"accountIds": [account_id], "query": query}],
           "platformOptions": {"ignoreTimeRange": True}}
    if viz in ("viz.bar", "viz.pie", "viz.table"):
        cfg["facet"] = {"showOtherSeries": False}
    return {"title": title, "layout": {"column": col, "row": row, "width": width, "height": height},
            "linkedEntityGuids": None, "visualization": {"id": viz}, "rawConfiguration": cfg}


def _markdown(text, col, row, width, height):
    return {"title": "", "layout": {"column": col, "row": row, "width": width, "height": height},
            "linkedEntityGuids": None, "visualization": {"id": "viz.markdown"},
            "rawConfiguration": {"text": text}}


def build_dashboard(customer: str, account_id: int, table: str,
                    full_light_days: int = 2, basic_heavy_days: int = 10) -> dict:
    T = f"lookup({table})"
    NF = f"WHERE feature != '{NO_FEATURE}'"
    w = lambda *a: _widget(*a, account_id)  # noqa: E731
    overview = [
        _markdown(f"## {customer}: New Relic usage by person and feature\n"
                  f"Data comes from the monthly MEGA user activity export, uploaded to the **{table}** "
                  "lookup table. Counts are page views by product area during the export window, so they "
                  "show what people used, not what their user type allows.", 1, 1, 12, 2),
        w("This report covers", "viz.table",
          f"SELECT uniqueCount(email) AS 'Users' FROM {T} FACET reportMonth, periodStart, periodEnd", 1, 3, 4, 2),
        w("Who is tracked", "viz.billboard",
          f"SELECT uniqueCount(email) AS 'Tracked users', "
          f"filter(uniqueCount(email), WHERE userType = 'FULL') AS 'FULL users', "
          f"filter(uniqueCount(email), WHERE userType = 'BASIC') AS 'BASIC users', "
          f"filter(uniqueCount(email), WHERE userType = 'CORE') AS 'CORE users' FROM {T}", 5, 3, 8, 2),
        w("People using each feature", "viz.bar",
          f"SELECT uniqueCount(email) AS 'People' FROM {T} {NF} FACET feature LIMIT 50", 1, 5, 6, 6),
        w("User types", "viz.pie", f"SELECT uniqueCount(email) FROM {T} FACET userType", 7, 5, 3, 3),
        w("How often people log in", "viz.bar",
          f"SELECT uniqueCount(email) AS 'People' FROM {T} FACET cases("
          "WHERE daysActive <= 1 AS '1 day', WHERE daysActive <= 4 AS '2-4 days', "
          "WHERE daysActive <= 9 AS '5-9 days', WHERE daysActive <= 14 AS '10-14 days', "
          "WHERE daysActive <= 19 AS '15-19 days', WHERE daysActive > 19 AS '20+ days')", 10, 5, 3, 3),
        w("Most-used accounts", "viz.bar",
          f"SELECT uniqueCount(email) AS 'People' FROM {T} FACET latestAccount LIMIT 10", 7, 8, 6, 3),
        w("Feature adoption by user type", "viz.table",
          f"SELECT uniqueCount(email) AS 'People', "
          f"filter(uniqueCount(email), WHERE userType = 'FULL') AS 'FULL', "
          f"filter(uniqueCount(email), WHERE userType = 'BASIC') AS 'BASIC', "
          f"sum(pageViews) AS 'Page views in feature' FROM {T} {NF} FACET feature LIMIT 50", 1, 11, 12, 5),
    ]
    people = [
        # No ORDER BY: in a FACET query NRQL rejects ASC/DESC ("unexpected 'DESC'"), and facets
        # are already picked and sorted descending by the first SELECT field, max(totalPageViews).
        # Views comes first: table widgets also sort by their first number column.
        # max() rather than latest(): lookup rows have no timestamp, and every row for a
        # person repeats the same totals, so max() is exact.
        w("Every user and what they use", "viz.table",
          f"SELECT max(totalPageViews) AS 'Views', max(daysActive) AS 'Days', "
          f"max(nrqlExecuted) AS 'NRQL queries' FROM {T} "
          "FACET userName AS 'User', email AS 'Email', userType AS 'User type', "
          "featuresUsed AS 'Features used (page views)' "
          "LIMIT MAX", 1, 1, 12, 7),
        w("Page views per person and feature", "viz.table",
          f"SELECT sum(pageViews) AS 'Page views' FROM {T} {NF} FACET userName, userType, feature LIMIT MAX",
          1, 8, 12, 6),
    ]
    fit = [
        _markdown("Two lists worth reviewing together. Activity alone doesn't decide someone's user type, "
                  "so treat these as a starting point. To change the thresholds, edit the number in each query.",
                  1, 1, 12, 1),
        w(f"FULL users active for {full_light_days} days or less", "viz.table",
          f"SELECT max(daysActive) AS 'Days active', max(totalPageViews) AS 'Page views' FROM {T} "
          f"WHERE userType = 'FULL' AND daysActive <= {full_light_days} FACET userName, email LIMIT MAX",
          1, 2, 6, 7),
        w(f"BASIC users active for {basic_heavy_days} days or more", "viz.table",
          f"SELECT max(daysActive) AS 'Days active', max(totalPageViews) AS 'Page views' FROM {T} "
          f"WHERE userType = 'BASIC' AND daysActive >= {basic_heavy_days} FACET userName, email LIMIT MAX",
          7, 2, 6, 7),
    ]
    return {
        "name": f"{customer} - New Relic usage by person and feature",
        "description": f"Monthly MEGA user activity export, loaded from the {table} lookup table.",
        "permissions": "PUBLIC_READ_ONLY",
        "pages": [
            {"name": "Overview", "description": None, "widgets": overview},
            {"name": "People", "description": None, "widgets": people},
            {"name": "User type vs. activity", "description": None, "widgets": fit},
        ],
        "variables": [],
    }


# ---------------------------------------------------------------- CLI

def fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def summarize(export: Export, customer: str) -> str:
    users = export.users
    by_type: Dict[str, int] = {}
    for u in users:
        by_type[u.user_type] = by_type.get(u.user_type, 0) + 1
    adoption = sorted(
        ((f, sum(1 for u in users if f in u.features)) for f in export.feature_columns),
        key=lambda kv: -kv[1])
    unused = [f for f, n in adoption if n == 0]
    start, end = export.period_start, export.period_end
    lines = [
        f"{customer}: {len(users):,} users "
        f"({', '.join(f'{n:,} {t}' for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]))})",
        f"Activity from {fmt_date(start)} to {fmt_date(end)}" if start and end else "Activity dates: not found",
        "Most-used features: " + ", ".join(f"{f} ({n / len(users):.0%})" for f, n in adoption[:5]),
    ]
    if unused:
        lines.append("No activity: " + ", ".join(unused))
    return "\n".join(lines)


def write_dashboard(args, customer: str, table: str, folder: Path) -> Path:
    account = args.account_id or PLACEHOLDER_ACCOUNT_ID
    suffix = "" if args.account_id else "_NEEDS_ACCOUNT_ID"
    path = folder / f"{safe_name(customer)}_Usage_Dashboard{suffix}.json"
    if args.account_id:  # a real ID replaces any earlier placeholder file
        (folder / f"{safe_name(customer)}_Usage_Dashboard_NEEDS_ACCOUNT_ID.json").unlink(missing_ok=True)
    path.write_text(json.dumps(build_dashboard(customer, account, table,
                                               args.full_light_days, args.basic_heavy_days),
                               indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def days_arg(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not 1 <= n <= 31:
        raise argparse.ArgumentTypeError("must be a whole number of days from 1 to 31")
    return n


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Build a New Relic usage report from a MEGA user-activity CSV export.")
    p.add_argument("export", type=Path, nargs="?",
                   help="MEGA 'Export - User Activity as CSV' file (not needed with --dashboard-only)")
    p.add_argument("--customer", required=True, help='Customer name as it should appear, e.g. "Acme"')
    p.add_argument("--previous", type=Path, help="Last month's export, to show changes in the report")
    p.add_argument("--account-id", type=int,
                   help="New Relic account the lookup table is in. Without it the dashboard JSON "
                        "uses placeholder account {PLACEHOLDER_ACCOUNT_ID}.".format(PLACEHOLDER_ACCOUNT_ID=PLACEHOLDER_ACCOUNT_ID))
    p.add_argument("--dashboard-only", action="store_true",
                   help="Only write the dashboard JSON (needs --customer and --account-id, no CSV)")
    p.add_argument("--out", type=Path, default=Path("output"), help="Output folder (default: ./output)")
    p.add_argument("--table-name", help="Override the lookup table name (default: <Customer>UserFeatureUsage)")
    p.add_argument("--full-light-days", type=days_arg, default=2,
                   help="Report and dashboard: FULL users active for this many days or less (default 2)")
    p.add_argument("--basic-heavy-days", type=days_arg, default=10,
                   help="Report and dashboard: BASIC users active for this many days or more (default 10)")
    args = p.parse_args(argv)
    customer = args.customer.strip()
    table = args.table_name or table_name(customer)

    if args.dashboard_only:
        if not args.account_id:
            p.error("--dashboard-only needs --account-id")
        args.out.mkdir(parents=True, exist_ok=True)
        dash = write_dashboard(args, customer, table, args.out)
        print(f"Wrote {dash} for account {args.account_id}, reading lookup({table}).")
        return 0
    if not args.export:
        p.error("give the MEGA export CSV (or use --dashboard-only)")

    try:
        current = read_export(args.export)
        previous = read_export(args.previous) if args.previous else None
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename}", file=sys.stderr)
        return 1
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    slug = safe_name(customer)
    month = current.report_month
    out_dir = args.out / f"{slug}_{month}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(summarize(current, customer))
    if previous:
        print(f"Comparing with {previous.path.name} ({len(previous.users):,} users)")
    print()

    report = out_dir / f"{slug}_NewRelic_Usage_{month}.html"
    report.write_text(render_html(current, customer, previous, table=table,
                                  full_light_days=args.full_light_days,
                                  basic_heavy_days=args.basic_heavy_days), encoding="utf-8")

    lookup = out_dir / f"{table}_{month}.csv"
    try:
        n_rows = write_lookup_csv(current, lookup)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    size = lookup.stat().st_size

    raw_copy = out_dir / f"{slug}_user_activity_{month}.csv"
    shutil.copyfile(args.export, raw_copy)

    written = [
        (report, "report: open in a browser, use Save as PDF to make the PDF"),
        (raw_copy, "raw MEGA export, renamed for sending to the customer"),
        (lookup, f"New Relic lookup table '{table}': {n_rows:,} rows, {size / 1024:,.0f} KB"),
    ]
    dash = write_dashboard(args, customer, table, out_dir)
    written.append((dash, f"New Relic dashboard for account {args.account_id}" if args.account_id
                    else f"New Relic dashboard with PLACEHOLDER account {PLACEHOLDER_ACCOUNT_ID}"))

    print(f"Wrote to {out_dir}/")
    for path, what in written:
        print(f"  {path.name}\n      {what}")

    if n_rows > LOOKUP_MAX_ROWS or size > LOOKUP_MAX_BYTES:
        print(f"\nwarning: the lookup CSV is over New Relic's limit (20,000 rows / 4 MB); "
              "it won't upload as one table.", file=sys.stderr)
    if not args.account_id:
        print(f"\n*** The dashboard JSON uses placeholder account {PLACEHOLDER_ACCOUNT_ID}; every widget "
              "will error until it's replaced. ***\n"
              "Once you know which account the lookup table is in, rebuild just the dashboard:\n"
              f'  python3 mega_report.py --dashboard-only --customer "{customer}" --account-id <ID> --out "{out_dir}"')
    print(f"\nNew Relic: upload {lookup.name} under Logs > Lookup tables as '{table}', "
          f"then check with:\n  SELECT count(*), uniqueCount(email) FROM lookup({table})\n"
          f"  (expect {n_rows:,} rows and {len(current.users):,} people)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
