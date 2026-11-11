"""Run with:  python3 -m unittest discover tests"""

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mega_report as mr  # noqa: E402

SAMPLE = ROOT / "sample" / "sample_export.csv"


def write_csv(folder: Path, name: str, rows) -> Path:
    p = folder / name
    with p.open("w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    return p


class ParsingTests(unittest.TestCase):
    def test_name_from_email(self):
        self.assertEqual(mr.name_from_email("Jane_Doe@example.com"), "Jane Doe")
        self.assertEqual(mr.name_from_email("mei.chen@example.com"), "Mei Chen")
        self.assertEqual(mr.name_from_email("J_Smith@example.com"), "J Smith")

    def test_parse_date(self):
        self.assertEqual(mr.parse_date("November 5, 2026"), date(2026, 11, 5))
        self.assertEqual(mr.parse_date("2026-10-06"), date(2026, 10, 6))
        self.assertIsNone(mr.parse_date("not a date"))

    def test_to_number(self):
        self.assertEqual(mr.to_number("12"), 12)
        self.assertEqual(mr.to_number(""), 0)
        self.assertEqual(mr.to_number("n/a"), 0)
        self.assertEqual(mr.to_number("2.5"), 2.5)

    def test_reads_sample(self):
        ex = mr.read_export(SAMPLE)
        self.assertEqual(len(ex.users), 40)
        self.assertIn("APM", ex.feature_columns)
        self.assertNotIn("Email", ex.feature_columns)
        self.assertEqual(ex.report_month, "2026-11")

    def test_missing_column_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_csv(Path(d), "bad.csv", [["Email", "APM"], ["a@x.com", "3"]])
            with self.assertRaises(mr.ExportError) as ctx:
                mr.read_export(p)
            self.assertIn("User Type", str(ctx.exception))

    def test_names_for_files_and_tables(self):
        self.assertEqual(mr.safe_name("Acme Inc."), "Acme_Inc")
        self.assertEqual(mr.table_name("Acme Inc."), "AcmeIncUserFeatureUsage")
        self.assertEqual(mr.table_name("7-Eleven"), "C7ElevenUserFeatureUsage")


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.ex = mr.read_export(SAMPLE)
        self.rows = mr.lookup_rows(self.ex)
        self.col = {c: i for i, c in enumerate(mr.LOOKUP_COLUMNS)}

    def test_every_nonzero_cell_appears_once(self):
        with SAMPLE.open(newline="") as fh:
            src = list(csv.DictReader(fh))
        expected = {(r["Email"], f, int(r[f])) for r in src for f in self.ex.feature_columns if int(r[f]) > 0}
        got = {(r[self.col["email"]], r[self.col["feature"]], r[self.col["pageViews"]])
               for r in self.rows if r[self.col["feature"]] != mr.NO_FEATURE}
        self.assertEqual(got, expected)

    def test_users_with_no_activity_keep_one_row(self):
        emails = {r[self.col["email"]] for r in self.rows}
        self.assertEqual(len(emails), len(self.ex.users))
        none_rows = [r for r in self.rows if r[self.col["feature"]] == mr.NO_FEATURE]
        self.assertEqual(len(none_rows), sum(1 for u in self.ex.users if not u.features))

    def test_features_used_is_per_person_and_sorted(self):
        by_email = {}
        for r in self.rows:
            by_email.setdefault(r[self.col["email"]], set()).add(r[self.col["featuresUsed"]])
        self.assertTrue(all(len(v) == 1 for v in by_email.values()), "one featuresUsed value per person")
        for u in self.ex.users:
            text = next(iter(by_email[u.email]))
            if not u.features:
                self.assertEqual(text, mr.NO_FEATURE_TEXT)
                continue
            counts = [int(part.rsplit("(", 1)[1].rstrip(")")) for part in text.split(", ")]
            self.assertEqual(counts, sorted(counts, reverse=True))
            self.assertEqual(len(counts), len(u.features))

    def test_features_used_tie_break_is_alphabetical(self):
        u = mr.User("a@x.com", "A", "FULL", 9, 0, 1, "", None, {"Logs": 3, "apm": 3, "Alerts": 5})
        self.assertEqual(mr.features_used(u), "Alerts (5), apm (3), Logs (3)")

    def test_long_feature_list_fits_255_bytes(self):
        feats = {f"Feature number {i:02d} with a long name": 100 - i for i in range(20)}
        u = mr.User("a@x.com", "A", "FULL", 9, 0, 1, "", None, feats)
        text = mr.features_used(u)
        self.assertLessEqual(len(text.encode("utf-8")), mr.LOOKUP_MAX_VALUE_BYTES)
        self.assertTrue(text.startswith("Feature number 00 with a long name (100)"))
        self.assertRegex(text, r", \+\d+ more$")
        kept = text.count(" (")
        self.assertEqual(int(text.rsplit("+", 1)[1].split()[0]), 20 - kept)

    def test_short_list_is_untouched(self):
        self.assertEqual(mr.fit_list(["APM (3)", "Logs (2)"], 255), "APM (3), Logs (2)")

    def test_every_value_within_limit(self):
        for r in self.rows:
            for v in r:
                self.assertLessEqual(len(str(v).encode("utf-8")), mr.LOOKUP_MAX_VALUE_BYTES)

    def test_written_file_has_header_and_limits(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.csv"
            n = mr.write_lookup_csv(self.ex, p)
            with p.open() as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines[0], ",".join(mr.LOOKUP_COLUMNS))
            self.assertEqual(len(lines) - 1, n)
            self.assertLess(n, mr.LOOKUP_MAX_ROWS)


class OutputTests(unittest.TestCase):
    def test_html_embeds_data_and_customer(self):
        ex = mr.read_export(SAMPLE)
        out = mr.render_html(ex, "Acme & Co")
        self.assertIn("Acme &amp; Co – New Relic usage report", out)
        self.assertIn('"table": "AcmeCoUserFeatureUsage"', out)
        self.assertIn("Noah_Kim32@example.com", out)
        for placeholder in ("__CUSTOMER_HTML__", "__CONFIG__", "__CSV_CURRENT__"):
            self.assertNotIn(placeholder, out)

    def test_html_with_previous_month(self):
        ex = mr.read_export(SAMPLE)
        out = mr.render_html(ex, "Acme", previous=ex)
        self.assertEqual(out.count("Noah_Kim32@example.com"), 2)

    def test_dashboard_uses_table_and_account(self):
        dash = mr.build_dashboard("Acme", 123, "AcmeUserFeatureUsage")
        queries = [w["rawConfiguration"]["nrqlQueries"][0] for p in dash["pages"] for w in p["widgets"]
                   if "nrqlQueries" in w["rawConfiguration"]]
        self.assertTrue(queries)
        for q in queries:
            self.assertEqual(q["accountIds"], [123])
            self.assertIn("lookup(AcmeUserFeatureUsage)", q["query"])
        json.dumps(dash)  # must be serialisable

    def test_dashboard_has_per_person_widget(self):
        dash = mr.build_dashboard("Acme", 123, "AcmeUserFeatureUsage")
        q = dash["pages"][1]["widgets"][0]["rawConfiguration"]["nrqlQueries"][0]["query"]
        self.assertIn("featuresUsed AS", q)
        self.assertIn("email AS", q)
        self.assertNotIn("DESC", q)  # NRQL rejects ASC/DESC in FACET queries
        self.assertNotIn("ORDER BY", q)
        self.assertTrue(q.startswith("SELECT max(totalPageViews)"))
        self.assertNotIn("latest(", q)

    def test_without_account_id_writes_placeholder_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mr.main([str(SAMPLE), "--customer", "Acme", "--out", d]), 0)
            folder = Path(d) / "Acme_2026-11"
            dash = json.loads((folder / "Acme_Usage_Dashboard_NEEDS_ACCOUNT_ID.json").read_text())
            ids = {i for p in dash["pages"] for w in p["widgets"]
                   for q in w["rawConfiguration"].get("nrqlQueries", []) for i in q["accountIds"]}
            self.assertEqual(ids, {mr.PLACEHOLDER_ACCOUNT_ID})
            # rebuilding with a real ID replaces the placeholder file
            self.assertEqual(mr.main(["--dashboard-only", "--customer", "Acme", "--account-id", "77",
                                      "--out", str(folder)]), 0)
            self.assertFalse((folder / "Acme_Usage_Dashboard_NEEDS_ACCOUNT_ID.json").exists())
            self.assertTrue((folder / "Acme_Usage_Dashboard.json").exists())

    def test_dashboard_only_requires_account_id(self):
        with self.assertRaises(SystemExit):
            mr.main(["--dashboard-only", "--customer", "Acme"])

    def test_report_uses_day_thresholds_and_table_name(self):
        ex = mr.read_export(SAMPLE)
        out = mr.render_html(ex, "Acme", table="acmeCustom", full_light_days=4, basic_heavy_days=8)
        self.assertIn('"table": "acmeCustom"', out)
        self.assertIn('"fullLightDays": 4', out)
        self.assertIn('"basicHeavyDays": 8', out)

    def test_day_options_reach_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mr.main([str(SAMPLE), "--customer", "Acme", "--account-id", "1",
                                      "--full-light-days", "4", "--basic-heavy-days", "8", "--out", d]), 0)
            dash = json.loads((Path(d) / "Acme_2026-11" / "Acme_Usage_Dashboard.json").read_text())
            queries = " ".join(w["rawConfiguration"]["nrqlQueries"][0]["query"]
                               for w in dash["pages"][2]["widgets"] if "nrqlQueries" in w["rawConfiguration"])
            self.assertIn("daysActive <= 4", queries)
            self.assertIn("daysActive >= 8", queries)

    def test_bad_day_values_are_rejected(self):
        for bad in ("0", "40", "abc"):
            with self.assertRaises(SystemExit):
                mr.main([str(SAMPLE), "--customer", "Acme", "--full-light-days", bad])

    def test_previous_month_is_embedded(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mr.main([str(SAMPLE), "--customer", "Acme", "--previous", str(SAMPLE),
                                      "--out", d]), 0)
            html = (Path(d) / "Acme_2026-11" / "Acme_NewRelic_Usage_2026-11.html").read_text()
            self.assertNotIn("__CSV_PREVIOUS__</script>", html.split("<script>")[0])

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            code = mr.main([str(SAMPLE), "--customer", "Acme", "--account-id", "123", "--out", d])
            self.assertEqual(code, 0)
            folder = Path(d) / "Acme_2026-11"
            names = sorted(p.name for p in folder.iterdir())
            self.assertEqual(names, [
                "AcmeUserFeatureUsage_2026-11.csv",
                "Acme_NewRelic_Usage_2026-11.html",
                "Acme_Usage_Dashboard.json",
                "Acme_user_activity_2026-11.csv",
            ])


if __name__ == "__main__":
    unittest.main()
