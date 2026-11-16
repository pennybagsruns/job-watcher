"""
fetch.py - daily raw-data collector for the job watcher.

For every source listed in check_sources.py, download ALL open postings and
save the raw API responses, gzipped, to:

    data/raw/<UTC date YYYY-MM-DD>/<source_slug>.json.gz

Each file holds {"source": ..., "fetched_at": ..., "count": ..., "pages": [...]}
where "pages" is the list of raw JSON responses exactly as the API sent them.

A summary of every source (name, ok, count, error) goes to
data/raw/<date>/_results.json. A source that fails writes no raw file.
Running again on the same UTC day replaces that day's folder completely.

Standard library only:
    python fetch.py
"""

import datetime as dt
import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from check_sources import SOURCES

TIMEOUT = 60
UA = "Mozilla/5.0 (job-watcher class project; daily fetch)"
RETRIES = 3            # attempts per HTTP request
PAGE_DELAY = 0.3       # seconds between page requests, to be polite
MAX_PAGES = 1000       # safety stop so a misbehaving API can't loop forever
WORKDAY_PAGE = 20
ORACLE_PAGE = 25

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(ROOT, "data", "raw")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_json(url, body=None):
    """GET (or POST JSON when body is given) and return parsed JSON, with retries."""
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code < 500 and e.code != 429:   # client error: retrying won't help
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last_err = e
        time.sleep(2 ** attempt)
    raise last_err


# ---------------------------------------------------------------------------
# Fetchers: each returns (pages, count) where pages is a list of raw responses.
# Paged sources keep going until a page comes back empty (or only repeats
# postings already seen) - the reported total is never trusted.
# ---------------------------------------------------------------------------
def fetch_workday(src):
    last_err = None
    for host in src["hosts"]:
        url = f"https://{host}/wday/cxs/{src['tenant']}/{src['site']}/jobs"
        try:
            return _page_workday(url, src.get("search", ""))
        except Exception as e:  # try the next host
            last_err = e
    raise last_err


def _page_workday(url, search):
    pages, seen = [], set()
    for page_no in range(MAX_PAGES):
        body = {"appliedFacets": {}, "limit": WORKDAY_PAGE,
                "offset": page_no * WORKDAY_PAGE, "searchText": search}
        data = http_json(url, body)
        posts = data.get("jobPostings") or []
        if not posts:
            break
        ids = {p.get("externalPath") or json.dumps(p, sort_keys=True) for p in posts}
        new = ids - seen
        if not new:
            break
        seen |= new
        pages.append(data)
        time.sleep(PAGE_DELAY)
    else:
        raise RuntimeError(f"hit MAX_PAGES ({MAX_PAGES}) without reaching the end")
    return pages, len(seen)


def fetch_oracle(src):
    base = f"https://{src['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    kw = f',keyword="{src["keyword"]}"' if src.get("keyword") else ""
    pages, seen = [], set()
    for page_no in range(MAX_PAGES):
        finder = (f"findReqs;siteNumber={src['site']},limit={ORACLE_PAGE},"
                  f"sortBy=POSTING_DATES_DESC,offset={page_no * ORACLE_PAGE}{kw}")
        url = (f"{base}?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder={urllib.parse.quote(finder, safe=';,=')}")
        data = http_json(url)
        items = data.get("items") or []
        reqs = (items[0].get("requisitionList") or []) if items else []
        if not reqs:
            break
        ids = {str(r.get("Id")) if r.get("Id") is not None else json.dumps(r, sort_keys=True)
               for r in reqs}
        new = ids - seen
        if not new:
            break
        seen |= new
        pages.append(data)
        time.sleep(PAGE_DELAY)
    else:
        raise RuntimeError(f"hit MAX_PAGES ({MAX_PAGES}) without reaching the end")
    return pages, len(seen)


def fetch_greenhouse(src):
    # The Greenhouse board API returns every job in one response.
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{src['token']}/jobs")
    return [data], len(data.get("jobs") or [])


def fetch_lever(src):
    # Lever's v0 postings API returns every posting in one response.
    data = http_json(f"https://api.lever.co/v0/postings/{src['company']}?mode=json")
    if not isinstance(data, list):
        raise ValueError("Lever response was not a list")
    return [data], len(data)


FETCHERS = {"workday": fetch_workday, "oracle": fetch_oracle,
            "greenhouse": fetch_greenhouse, "lever": fetch_lever}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def slugify(name):
    """'Houlihan Lokey (Campus)' -> 'houlihan_lokey_campus'"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def write_gz_json(path, obj):
    # mtime=0 keeps the gzip header stable so identical data gives identical bytes.
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def main():
    now = dt.datetime.now(dt.timezone.utc)
    day = now.strftime("%Y-%m-%d")
    day_dir = os.path.join(RAW_DIR, day)
    tmp_dir = day_dir + ".tmp"

    # Build the day's files in a scratch folder, then swap it in at the end, so
    # a rerun fully replaces the day (including dropping files of sources that
    # succeeded earlier but fail now).
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)

    slugs = [slugify(s["name"]) for s in SOURCES]
    if len(set(slugs)) != len(slugs):
        raise SystemExit("Two sources share the same slug; rename one in check_sources.py")

    results = []
    for src, slug in zip(SOURCES, slugs):
        started = time.time()
        result = {"name": src["name"], "slug": slug, "ats": src["ats"],
                  "ok": False, "count": 0, "error": None}
        try:
            pages, count = FETCHERS[src["ats"]](src)
            write_gz_json(os.path.join(tmp_dir, f"{slug}.json.gz"), {
                "source": src,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "count": count,
                "pages": pages,
            })
            result.update(ok=True, count=count)
        except urllib.error.HTTPError as e:
            result["error"] = f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        result["seconds"] = round(time.time() - started, 1)
        results.append(result)
        status = "OK  " if result["ok"] else "FAIL"
        print(f"{status} {src['name']:<26} {result['count']:>6}  {result['error'] or ''}",
              flush=True)

    with open(os.path.join(tmp_dir, "_results.json"), "w", encoding="utf-8") as f:
        json.dump({"date": day, "run_at": now.isoformat(timespec="seconds"),
                   "results": results}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    shutil.rmtree(day_dir, ignore_errors=True)
    os.replace(tmp_dir, day_dir)

    ok = sum(r["ok"] for r in results)
    print(f"{ok} of {len(results)} sources fetched -> {os.path.relpath(day_dir, ROOT)}")
    return 0 if ok else 1   # fail the run only if every source failed


if __name__ == "__main__":
    sys.exit(main())
