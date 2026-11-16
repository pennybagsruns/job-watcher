"""
status.py - write data/status.json from history.csv and the raw folders.

Run after build.py:
    python status.py
"""

import datetime as dt
import json
import os
import sys

import build

STATUS_JSON = os.path.join(build.DATA_DIR, "status.json")


def todays_changes(rows, day):
    """(new, removed) postings for the run on `day`. New excludes first_run postings."""
    new = [r for r in rows if r["first_seen"] == day and r["first_run"] == "false"]
    removed = [r for r in rows if r["status"] == "removed" and r["removed_date"] == day]
    return new, removed


def latest_results(day):
    path = os.path.join(build.RAW_DIR, day, "_results.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        results = json.load(f).get("results", [])
    return [{"name": r["name"], "ok": bool(r["ok"]), "count": int(r.get("count") or 0)}
            for r in results]


def main():
    days_with_files = [d for d in build.date_dirs() if build.raw_files(d)]
    latest = build.latest_run_date()
    if not latest:
        print("No raw data folders found under data/raw", file=sys.stderr)
        return 1

    rows = build.read_history()
    new, removed = todays_changes(rows, latest)
    status = {
        "run_time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_run_date": days_with_files[0] if days_with_files else None,
        "days_with_success": len(days_with_files),
        "open": sum(r["status"] == "open" for r in rows),
        "new_today": len(new),
        "removed_today": len(removed),
        "sources": latest_results(latest),
    }
    tmp = STATUS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, STATUS_JSON)

    ok = sum(s["ok"] for s in status["sources"])
    print(f"status.json: latest run {latest}, {status['open']} open, "
          f"{status['new_today']} new, {status['removed_today']} removed, "
          f"{ok}/{len(status['sources'])} sources ok, {status['days_with_success']} day(s) with data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
