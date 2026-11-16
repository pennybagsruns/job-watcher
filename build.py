"""
build.py - rebuild data/history.csv from the raw files only.

Reads every data/raw/<YYYY-MM-DD>/<source_slug>.json.gz in date order and
replays them to work out, for every posting ever seen, when it was first and
last listed and whether it is still open. Nothing else is read, so running it
twice on the same raw files gives a byte-identical history.csv.

    python build.py
"""

import csv
import datetime as dt
import gzip
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")

COLUMNS = ["posting_id", "source", "source_id", "employer", "title", "location",
           "department", "posted_date", "url", "in_role", "first_run",
           "first_seen", "last_seen", "status", "removed_date"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# The in-role rule (one place only).
# ---------------------------------------------------------------------------
ROLE_INCLUDE = ("wealth", "private bank", "private client", "portfolio",
                "investment analyst", "investment associate", "financial analyst",
                "finance analyst", "fp&a", "financial planning", "financial advisor",
                "wealth advisor", "equity research", "investment banking", "valuation",
                "capital markets", "asset management", "trust officer")
ROLE_EXCLUDE = ("director", "vice president", "vp", "head of", "chief", "manager",
                "intern", "engineer")


def is_in_role(title):
    """True if the title has an include phrase and no exclude phrase (case-insensitive substring match)."""
    t = (title or "").lower()
    return any(k in t for k in ROLE_INCLUDE) and not any(k in t for k in ROLE_EXCLUDE)


# ---------------------------------------------------------------------------
# Raw-file discovery (shared with status.py and issue.py)
# ---------------------------------------------------------------------------
def date_dirs():
    """Sorted list of YYYY-MM-DD folder names under data/raw."""
    if not os.path.isdir(RAW_DIR):
        return []
    return sorted(d for d in os.listdir(RAW_DIR)
                  if DATE_RE.match(d) and os.path.isdir(os.path.join(RAW_DIR, d)))


def raw_files(day):
    """Sorted (slug, path) pairs of the raw files saved on one day."""
    folder = os.path.join(RAW_DIR, day)
    return [(f[:-len(".json.gz")], os.path.join(folder, f))
            for f in sorted(os.listdir(folder)) if f.endswith(".json.gz")]


def latest_run_date():
    """Date of the most recent fetch run (latest folder, even if every source failed)."""
    days = date_dirs()
    return days[-1] if days else None


# ---------------------------------------------------------------------------
# Per-board parsers: raw file -> list of posting dicts (without history fields)
# ---------------------------------------------------------------------------
def _iso_to_utc_date(value):
    if not value:
        return ""
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc)
    return d.strftime("%Y-%m-%d")


def _plain_date(value):
    value = (value or "")[:10]
    return value if DATE_RE.match(value) else ""


def parse_workday(src, pages):
    host = src["hosts"][0]
    base = f"https://{host}/en-US/{src['site']}"
    out = []
    for page in pages:
        for p in page.get("jobPostings") or []:
            path = p.get("externalPath") or ""
            bullets = [b for b in (p.get("bulletFields") or []) if isinstance(b, str)]
            source_id = ""
            if path:
                # The path ends in "_<job id>", e.g. ".../Portfolio-Associate_JR037563-1".
                # Prefer a bullet field the path ends with (keeps ids like "R26_2726-1").
                for b in bullets:
                    m = re.search(r"_(" + re.escape(b) + r"(?:-\d+)?)$", path)
                    if m:
                        source_id = m.group(1)
                        break
                if not source_id:
                    source_id = path.rsplit("_", 1)[-1]
                url = base + path
            elif bullets:
                # Some Workday entries come back as bare stubs (id only, no title or
                # path). No direct link exists, so link the board's search for the id.
                source_id = bullets[0]
                url = f"{base}?q={source_id}"
            else:
                continue
            location = p.get("locationsText") or ""
            if not location:
                # Some boards (e.g. Raymond James) put the office in a bullet field
                # instead; skip the id itself and "Label: value" bullets.
                location = next((b for b in bullets
                                 if b not in source_id and ":" not in b), "")
            out.append({
                "source_id": source_id,
                "title": p.get("title") or "",
                "location": location,
                "department": "",
                "posted_date": "",          # Workday only gives "Posted 3 Days Ago"
                "url": url,
            })
    return out


def parse_oracle(src, pages):
    base = f"https://{src['host']}/hcmUI/CandidateExperience/en/sites/{src['site']}/job/"
    out = []
    for page in pages:
        for item in page.get("items") or []:
            for r in item.get("requisitionList") or []:
                if r.get("Id") is None:
                    continue
                rid = str(r["Id"])
                out.append({
                    "source_id": rid,
                    "title": r.get("Title") or "",
                    "location": r.get("PrimaryLocation") or "",
                    "department": r.get("Department") or r.get("Organization") or "",
                    "posted_date": _plain_date(r.get("PostedDate")),
                    "url": base + rid,
                })
    return out


def parse_greenhouse(src, pages):
    out = []
    for page in pages:
        for j in page.get("jobs") or []:
            if j.get("id") is None:
                continue
            depts = [d.get("name") for d in (j.get("departments") or []) if d.get("name")]
            out.append({
                "source_id": str(j["id"]),
                "title": j.get("title") or "",
                "location": (j.get("location") or {}).get("name") or "",
                "department": "; ".join(depts),
                "posted_date": _iso_to_utc_date(j.get("first_published")),
                "url": j.get("absolute_url") or "",
            })
    return out


def parse_lever(src, pages):
    out = []
    for page in pages:
        for j in page or []:
            if not j.get("id"):
                continue
            cats = j.get("categories") or {}
            created = j.get("createdAt")
            posted = ""
            if isinstance(created, (int, float)):
                posted = dt.datetime.fromtimestamp(created / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
            out.append({
                "source_id": j["id"],
                "title": j.get("text") or "",
                "location": cats.get("location") or "",
                "department": cats.get("department") or cats.get("team") or "",
                "posted_date": posted,
                "url": j.get("hostedUrl") or f"https://jobs.lever.co/{src['company']}/{j['id']}",
            })
    return out


PARSERS = {"workday": parse_workday, "oracle": parse_oracle,
           "greenhouse": parse_greenhouse, "lever": parse_lever}


def employer_name(source_name):
    """'Houlihan Lokey (Campus)' -> 'Houlihan Lokey'"""
    return re.sub(r"\s*\([^)]*\)\s*$", "", source_name).strip()


def read_raw_file(slug, path):
    """Return {posting_id: posting dict} for one raw file (first listing wins on duplicates)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    src = raw["source"]
    postings = {}
    for p in PARSERS[src["ats"]](src, raw.get("pages") or []):
        pid = f"{slug}:{p['source_id']}"
        if pid in postings:
            continue
        p.update(posting_id=pid, source=slug, employer=employer_name(src["name"]),
                 in_role="true" if is_in_role(p["title"]) else "false")
        postings[pid] = p
    return postings


# ---------------------------------------------------------------------------
# Replay the raw files day by day
# ---------------------------------------------------------------------------
def build_history():
    rows = {}             # posting_id -> row dict
    by_source = {}        # source slug -> set of posting_ids
    first_file_day = {}   # source slug -> date of its first raw file

    for day in date_dirs():
        for slug, path in raw_files(day):
            listed = read_raw_file(slug, path)
            first_file_day.setdefault(slug, day)
            ids = by_source.setdefault(slug, set())

            for pid, p in listed.items():
                row = rows.get(pid)
                if row is None:
                    row = {"first_run": "true" if day == first_file_day[slug] else "false",
                           "first_seen": day}
                    rows[pid] = row
                    ids.add(pid)
                # Latest listing wins for the descriptive fields.
                row.update(p)
                row["last_seen"] = day
                row["status"] = "open"
                row["removed_date"] = ""

            # The source has a file today, so anything open it no longer lists is removed.
            for pid in ids:
                row = rows[pid]
                if row["status"] == "open" and pid not in listed:
                    row["status"] = "removed"
                    row["removed_date"] = day
    return [rows[pid] for pid in sorted(rows)]


def write_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HISTORY_CSV + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, HISTORY_CSV)


def read_history():
    """Load history.csv as a list of dicts (used by status.py and issue.py)."""
    with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main():
    days = date_dirs()
    if not days:
        print("No raw data folders found under data/raw", file=sys.stderr)
        return 1
    rows = build_history()
    write_history(rows)
    open_n = sum(r["status"] == "open" for r in rows)
    print(f"history.csv: {len(rows)} postings ({open_n} open, {len(rows) - open_n} removed) "
          f"from {len(days)} day(s), {days[0]} to {days[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
