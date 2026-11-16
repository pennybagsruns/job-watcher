"""
check_sources.py - test every candidate job board before it goes on the watch list.

Run it on your own computer (needs internet, no extra packages):
    python3 check_sources.py

For each employer it prints whether the job board answered with JSON (no login),
how many postings it has, and one sample title. Any board over 5,000 postings
needs a search term (the project rules say so); the script flags those.
"""

import json
import sys
import urllib.request
import urllib.error

TIMEOUT = 30
UA = "Mozilla/5.0 (job-watcher class project; source check)"

# ---------------------------------------------------------------------------
# Candidate sources. tier: reach / target / safety. Edit freely.
# Workday:   tenant, list of hosts to try, site
# Oracle:    host, site number, optional keyword
# Greenhouse: board token   Lever: company slug
# ---------------------------------------------------------------------------
SOURCES = [
    # --- Reach ---
    {"name": "Morgan Stanley", "tier": "reach", "ats": "workday",
     "tenant": "ms", "hosts": ["ms.wd5.myworkdayjobs.com"], "site": "External"},
    {"name": "JPMorganChase", "tier": "reach", "ats": "oracle",
     "host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "keyword": "analyst"},
    {"name": "Houlihan Lokey (Campus)", "tier": "reach", "ats": "workday",
     "tenant": "hl", "hosts": ["hl.wd1.myworkdayjobs.com"], "site": "Campus"},
    {"name": "Houlihan Lokey (Lateral)", "tier": "reach", "ats": "workday",
     "tenant": "hl", "hosts": ["hl.wd1.myworkdayjobs.com"], "site": "Lateral"},

    # --- Target ---
    {"name": "Fidelity Investments", "tier": "target", "ats": "workday",
     "tenant": "fmr", "hosts": ["fmr.wd1.myworkdayjobs.com"], "site": "FidelityCareers"},
    {"name": "Northern Trust", "tier": "target", "ats": "workday",
     "tenant": "ntrs", "hosts": ["ntrs.wd1.myworkdayjobs.com"], "site": "northerntrust"},
    {"name": "Raymond James", "tier": "target", "ats": "workday",
     "tenant": "raymondjames", "hosts": ["raymondjames.wd1.myworkdayjobs.com"],
     "site": "RaymondJamesCareers"},
    {"name": "Wells Fargo", "tier": "target", "ats": "workday",
     "tenant": "wf", "hosts": ["wf.wd1.myworkdayjobs.com"], "site": "WellsFargoJobs"},
    {"name": "Texas Capital", "tier": "target", "ats": "workday",
     "tenant": "texascapitalbank", "hosts": ["texascapitalbank.wd12.myworkdayjobs.com"],
     "site": "Careers"},
    {"name": "Mercer Advisors", "tier": "target", "ats": "greenhouse",
     "token": "merceradvisors"},
    {"name": "Brighton Jones", "tier": "target", "ats": "lever",
     "company": "brightonjones"},
    {"name": "Capital One", "tier": "target", "ats": "workday",
     "tenant": "capitalone", "hosts": ["capitalone.wd12.myworkdayjobs.com"],
     "site": "Capital_One"},

    # --- Safety / NWA anchors ---
    {"name": "Walmart", "tier": "safety", "ats": "workday",
     "tenant": "walmart",
     "hosts": ["walmart.wd5.myworkdayjobs.com", "walmart.wd504.myworkdayjobs.com"],
     "site": "WalmartExternal", "search": "finance"},
    {"name": "Tyson Foods", "tier": "safety", "ats": "workday",
     "tenant": "tysonfoods", "hosts": ["tysonfoods.wd5.myworkdayjobs.com"], "site": "TSN"},
    {"name": "J.B. Hunt", "tier": "safety", "ats": "workday",
     "tenant": "jbhunt",
     "hosts": ["jbhunt.wd501.myworkdayjobs.com", "jbhunt.wd5.myworkdayjobs.com"],
     "site": "Careers"},
    {"name": "Ameriprise Financial", "tier": "safety", "ats": "workday",
     "tenant": "ameriprise", "hosts": ["ameriprise.wd5.myworkdayjobs.com"],
     "site": "Ameriprise"},
    {"name": "Allworth Financial", "tier": "safety", "ats": "greenhouse",
     "token": "allworthfinancial"},
    {"name": "Farther", "tier": "safety", "ats": "greenhouse",
     "token": "fartherfinance"},
    {"name": "Bellwether", "tier": "safety", "ats": "lever",
     "company": "bellwetheram-2"},
]


def http_json(url, body=None):
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_workday(src):
    last_err = None
    for host in src["hosts"]:
        url = f"https://{host}/wday/cxs/{src['tenant']}/{src['site']}/jobs"
        body = {"appliedFacets": {}, "limit": 20, "offset": 0,
                "searchText": src.get("search", "")}
        try:
            data = http_json(url, body)
            posts = data.get("jobPostings", [])
            sample = posts[0].get("title", "") if posts else ""
            return data.get("total", len(posts)), sample, url
        except Exception as e:  # try the next host
            last_err = e
    raise last_err


def check_oracle(src):
    kw = f',keyword="{src["keyword"]}"' if src.get("keyword") else ""
    url = (f"https://{src['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList.secondaryLocations"
           f"&finder=findReqs;siteNumber={src['site']},limit=25,"
           f"sortBy=POSTING_DATES_DESC,offset=0{kw}")
    data = http_json(url)
    item = data["items"][0]
    reqs = item.get("requisitionList", [])
    sample = reqs[0].get("Title", "") if reqs else ""
    return item.get("TotalJobsCount", len(reqs)), sample, url


def check_greenhouse(src):
    url = f"https://boards-api.greenhouse.io/v1/boards/{src['token']}/jobs"
    data = http_json(url)
    jobs = data.get("jobs", [])
    sample = jobs[0].get("title", "") if jobs else ""
    return data.get("meta", {}).get("total", len(jobs)), sample, url


def check_lever(src):
    url = f"https://api.lever.co/v0/postings/{src['company']}?mode=json"
    data = http_json(url)
    sample = data[0].get("text", "") if data else ""
    return len(data), sample, url


CHECKERS = {"workday": check_workday, "oracle": check_oracle,
            "greenhouse": check_greenhouse, "lever": check_lever}


def main():
    ok_count = 0
    print(f"{'EMPLOYER':<26} {'TIER':<7} {'ATS':<11} {'RESULT':<7} {'POSTINGS':>8}  SAMPLE / ERROR")
    print("-" * 100)
    for src in SOURCES:
        try:
            total, sample, _ = CHECKERS[src["ats"]](src)
            ok_count += 1
            flag = "  <-- over 5,000: add a search term" if total and total > 5000 else ""
            print(f"{src['name']:<26} {src['tier']:<7} {src['ats']:<11} {'OK':<7} "
                  f"{total:>8}  {sample[:45]}{flag}")
        except urllib.error.HTTPError as e:
            print(f"{src['name']:<26} {src['tier']:<7} {src['ats']:<11} {'FAIL':<7} "
                  f"{'-':>8}  HTTP {e.code}")
        except Exception as e:
            print(f"{src['name']:<26} {src['tier']:<7} {src['ats']:<11} {'FAIL':<7} "
                  f"{'-':>8}  {type(e).__name__}: {str(e)[:50]}")
    print("-" * 100)
    print(f"{ok_count} of {len(SOURCES)} sources answered with JSON. You need at least 15.")
    return 0 if ok_count >= 15 else 1


if __name__ == "__main__":
    sys.exit(main())
