"""
Prepare a local clone of microsoft/Nonprofits for the Inspirit Oslo demo (4 Nov 2026).

Run this BEFORE Install-IntoWorkspace.ps1. The installer uploads the sample CSVs from the
local clone, and the orchestration pipeline re-imports them from OneLake on every run
(MERGE ... WHEN MATCHED UPDATE), so fixes must live in the CSV files, not only in the tables.

What it changes, all inside NonprofitDataSolutions/FabricWorkload/Workload/app/assets/items/
PackageInstallerItem/Fundraising:

1. Sample data dates: shifts every *Date column (except BirthDate) by a whole number of days,
   so that the latest gift lands on --target-latest (default 2026-10-28). Relative order and
   time-of-day are preserved. Segments are computed against today's date, so without this the
   New and Active segments are empty in November 2026.
2. Unsuitable sample values: renames the account "Sexy Babe" and neutralises four email
   addresses (marijuana..., drug..., kill..., booty...). IDs are untouched, so references hold.
3. Currency: Configuration.csv and the default-configuration notebook set NOK / "kr" after the
   amount. Fiscal year start stays at January (the sample Configuration.csv already sets 1).
4. Lifetime giving bands: relabels the ten bands from "$" to "kr" in BOTH notebooks that use them
   (DefaultConfig writes the names; CreateSegments maps the names to thresholds). Thresholds are
   unchanged; sample amounts are fictitious and read plausibly as NOK.
5. Semantic model: replaces the hard-coded "$" format strings (10 columns) with "#,0 kr".
   The report does not read the Configuration table for currency, so this is required.

Usage:
    python3 -I prepare_nds_demo.py /path/to/Nonprofits [--target-latest 2026-10-28] [--dry-run]
"""
import argparse
import csv
import io
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PKG = Path("NonprofitDataSolutions/FabricWorkload/Workload/app/assets/items/PackageInstallerItem/Fundraising")
SAMPLE = PKG / "data/nds-silver-sampledata"
NB = PKG / "definitions/Notebooks"
SM = PKG / "definitions/SemanticModels/Fundraising_Intelligence_Semantic.SemanticModel/definition/tables"

KEEP_DATE_COLUMNS = {"BirthDate"}

ACCOUNT_RENAMES = {"Sexy Babe": "Fjordlys Media"}
EMAIL_LOCALPART_FIXES = {"marijuana": "member", "drug": "donor", "kill": "giver", "booty": "friend"}

CONFIG_VALUES = {"Currency": "NOK", "CurrencySymbol": "kr", "IsCurrencySymbolPrefix": "False",
                 "FiscalYearStartMonth": "1", "DefaultLanguage": "en"}

BAND_LABELS = {
    "<$250": "< 250 kr",
    "$250–$999": "250–999 kr",
    "$1,000–$4,999": "1 000–4 999 kr",
    "$5,000–$9,999": "5 000–9 999 kr",
    "$10,000–$24,999": "10 000–24 999 kr",
    "$25,000–$49,999": "25 000–49 999 kr",
    "$50,000–$99,999": "50 000–99 999 kr",
    "$100,000–$499,999": "100 000–499 999 kr",
    "$500,000–$999,999": "500 000–999 999 kr",
    "$1,000,000+": "1 000 000+ kr",
}

KR_FORMAT = "#,0 kr;-#,0 kr;#,0 kr"

DATE_PATTERNS = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+$"), "%Y-%m-%dT%H:%M:%S.%f"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"), "%Y-%m-%dT%H:%M:%S"),
    (re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+$"), "%Y-%m-%d %H:%M:%S.%f"),
    (re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "%Y-%m-%d"),
]


def shift_value(value: str, days: int) -> str:
    for pattern, fmt in DATE_PATTERNS:
        if pattern.match(value):
            frac_len = len(value.split(".")[-1]) if "." in value else 0
            out = (datetime.strptime(value, fmt) + timedelta(days=days)).strftime(fmt)
            if frac_len and frac_len != 6:  # keep the original fractional precision
                head, frac = out.rsplit(".", 1)
                out = f"{head}.{(frac + '0' * frac_len)[:frac_len]}"
            return out
    raise ValueError(f"Unrecognised date format: {value!r}")


def read_csv(path: Path):
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return reader.fieldnames, list(reader), bom, newline


def write_csv(path: Path, fields, rows, bom, newline):
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator=newline)
    writer.writeheader()
    writer.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def compute_offset(root: Path, target: date) -> int:
    _, rows, _, _ = read_csv(root / SAMPLE / "Transaction.csv")
    latest = max(datetime.fromisoformat(r["TransactionDate"][:10]).date() for r in rows if r["TransactionDate"])
    return (target - latest).days


def fix_csvs(root: Path, days: int, dry: bool, log):
    for path in sorted((root / SAMPLE).glob("*.csv")):
        fields, rows, bom, newline = read_csv(path)
        changed = 0
        date_cols = [c for c in fields if "Date" in c and c not in KEEP_DATE_COLUMNS]
        for r in rows:
            for c in date_cols:
                if r[c]:
                    r[c] = shift_value(r[c], days)
                    changed += 1
            if path.name == "Account.csv" and r.get("Name") in ACCOUNT_RENAMES:
                log(f"  {path.name}: renamed account {r['Name']!r} -> {ACCOUNT_RENAMES[r['Name']]!r}")
                r["Name"] = ACCOUNT_RENAMES[r["Name"]]
                changed += 1
            for c in fields:
                if "Email" in c and r[c]:
                    local, _, domain = r[c].partition("@")
                    for bad, good in EMAIL_LOCALPART_FIXES.items():
                        if re.fullmatch(rf"{bad}\d+", local):
                            new = f"{good}{local[len(bad):]}@{domain}"
                            log(f"  {path.name}: {c} {r[c]} -> {new}")
                            r[c] = new
                            changed += 1
            if path.name == "Configuration.csv" and r["Name"] in CONFIG_VALUES:
                if r["Value"] != CONFIG_VALUES[r["Name"]]:
                    log(f"  Configuration.csv: {r['Name']} {r['Value']!r} -> {CONFIG_VALUES[r['Name']]!r}")
                    r["Value"] = CONFIG_VALUES[r["Name"]]
                    changed += 1
        if changed:
            log(f"{path.name}: {changed} values changed ({len(date_cols)} date columns shifted)")
            if not dry:
                write_csv(path, fields, rows, bom, newline)


def replace_in_file(path: Path, pairs, dry: bool, log, expect_each=True):
    text = path.read_text(encoding="utf-8")
    original = text
    for old, new in pairs:
        variants = [(old, new), (old.replace("–", "\\u2013"), new.replace("–", "\\u2013"))]
        hits = 0
        for o, n in variants:
            hits += text.count(o)
            text = text.replace(o, n)
        if expect_each and hits == 0:
            raise SystemExit(f"Expected to find {old!r} in {path} but did not. Repository changed? Stopping.")
    if text != original:
        log(f"{path.relative_to(path.parents[3])}: updated")
        if not dry:
            path.write_text(text, encoding="utf-8")


def fix_notebooks(root: Path, dry: bool, log):
    default_cfg = root / NB / "Fundraising_SL_DefaultConfig.Notebook/notebook-content.ipynb"
    segments = root / NB / "Fundraising_GD_CreateSegments.Notebook/notebook-content.ipynb"
    # Notebook sources are JSON strings, so quotes appear escaped (\") in the raw file.
    bands = [(f'\\"{k}\\"', f'\\"{v}\\"') for k, v in BAND_LABELS.items()]
    cfg = [('\\"Currency\\", \\"USD\\"', '\\"Currency\\", \\"NOK\\"'),
           ('\\"CurrencySymbol\\", \\"$\\"', '\\"CurrencySymbol\\", \\"kr\\"'),
           ('\\"FiscalYearStartMonth\\", \\"7\\"', '\\"FiscalYearStartMonth\\", \\"1\\"'),
           ('\\"IsCurrencySymbolPrefix\\", \\"true\\"', '\\"IsCurrencySymbolPrefix\\", \\"false\\"')]
    replace_in_file(default_cfg, bands + cfg, dry, log)
    replace_in_file(segments, bands, dry, log)


def fix_semantic_model(root: Path, dry: bool, log):
    pattern = re.compile(r"(formatString:\s*)\\\$#,0\.#+;[^\n]*")
    total = 0
    for path in sorted((root / SM).glob("*.tmdl")):
        text = path.read_text(encoding="utf-8")
        new, n = pattern.subn(lambda m: m.group(1) + KR_FORMAT, text)
        if n:
            total += n
            log(f"{path.name}: {n} currency format(s) -> kr")
            if not dry:
                path.write_text(new, encoding="utf-8")
    if total == 0:
        raise SystemExit("No '$' format strings found in the semantic model. Repository changed? Stopping.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--target-latest", default="2026-10-28", help="date the latest sample gift should land on")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.repo / SAMPLE).is_dir():
        sys.exit(f"Not a microsoft/Nonprofits clone: {a.repo}")
    days = compute_offset(a.repo, date.fromisoformat(a.target_latest))
    print(f"Shifting sample dates by {days} days (latest gift -> {a.target_latest}){' [dry run]' if a.dry_run else ''}")
    fix_csvs(a.repo, days, a.dry_run, print)
    fix_notebooks(a.repo, a.dry_run, print)
    fix_semantic_model(a.repo, a.dry_run, print)
    print("Done. Commit these changes to the Inspirit fork, then run Install-IntoWorkspace.ps1.")


if __name__ == "__main__":
    main()
