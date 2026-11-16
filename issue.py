"""
issue.py - open (or update) the daily GitHub Issue for the latest run.

The issue is titled with the run date (YYYY-MM-DD) and lists that day's new
and removed postings, each with a link. An issue is opened every day even if
nothing changed. Older daily issues still open are closed. Running again on
the same day edits that day's issue instead of opening another.

Run after build.py and status.py. Needs GITHUB_TOKEN and GITHUB_REPOSITORY
(both set automatically in GitHub Actions). Without them it just prints the
issue body (a dry run):
    python issue.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

import build
import status as status_mod

API = "https://api.github.com"
LABEL = "daily-report"
BODY_LIMIT = 60000   # GitHub's hard limit is 65,536 characters


# ---------------------------------------------------------------------------
# Issue body
# ---------------------------------------------------------------------------
def _md(text):
    """Escape characters that would break a Markdown link label."""
    return (text or "(no title)").replace("[", "(").replace("]", ")").replace("|", "/")


def _line(r):
    star = "⭐ " if r["in_role"] == "true" else ""
    where = f" · {_md(r['location'])}" if r["location"] else ""
    return f"- {star}[{_md(r['title'])}]({r['url']}) · {_md(r['employer'])}{where}"


def _sorted(rows):
    # In-role first, then employer and title, so truncation drops the least useful lines.
    return sorted(rows, key=lambda r: (r["in_role"] != "true", r["employer"], r["title"], r["posting_id"]))


def make_body(day, new, removed, status):
    head = [
        f"Job watcher report for **{day}** (UTC).",
        "",
        f"- New postings: **{len(new)}** ({sum(r['in_role'] == 'true' for r in new)} in role)",
        f"- Removed postings: **{len(removed)}** ({sum(r['in_role'] == 'true' for r in removed)} in role)",
        f"- Open postings: **{status['open']}**",
        f"- Sources OK: **{sum(s['ok'] for s in status['sources'])} of {len(status['sources'])}**",
    ]
    failed = [s["name"] for s in status["sources"] if not s["ok"]]
    if failed:
        head.append(f"- Failed sources: {', '.join(failed)}")
    head += ["", "⭐ = matches my role."]

    sections = [("New postings", _sorted(new)), ("Removed postings", _sorted(removed))]
    budget = BODY_LIMIT - len("\n".join(head)) - 500
    per_section = budget // 2
    parts = list(head)
    for title, rows in sections:
        parts += ["", f"## {title} ({len(rows)})", ""]
        if not rows:
            parts.append("_None today._")
            continue
        used = 0
        for i, r in enumerate(rows):
            line = _line(r)
            if used + len(line) + 1 > per_section:
                parts.append(f"- …and {len(rows) - i} more (see data/history.csv)")
                break
            parts.append(line)
            used += len(line) + 1
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
def gh(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "job-watcher",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub {method} {path} -> HTTP {e.code}: {e.read()[:300]!r}") from None


def list_issues(repo, token, state, per_page=100):
    # The issues endpoint also returns pull requests; drop those.
    items = gh("GET", f"/repos/{repo}/issues?labels={LABEL}&state={state}"
                      f"&sort=created&direction=desc&per_page={per_page}", token)
    return [i for i in items if "pull_request" not in i]


def publish(repo, token, day, body):
    open_issues = list_issues(repo, token, "open")
    today = next((i for i in open_issues if i["title"] == day), None)
    if today is None:
        # Maybe today's issue was already made and then closed; reopen it rather than duplicate.
        today = next((i for i in list_issues(repo, token, "all") if i["title"] == day), None)

    if today:
        gh("PATCH", f"/repos/{repo}/issues/{today['number']}", token,
           {"body": body, "state": "open"})
        print(f"Updated issue #{today['number']} ({day})")
        number = today["number"]
    else:
        created = gh("POST", f"/repos/{repo}/issues", token,
                     {"title": day, "body": body, "labels": [LABEL]})
        print(f"Opened issue #{created['number']} ({day})")
        number = created["number"]

    for i in open_issues:
        if i["number"] != number and i["title"] < day:
            gh("PATCH", f"/repos/{repo}/issues/{i['number']}", token,
               {"state": "closed", "state_reason": "completed"})
            print(f"Closed older issue #{i['number']} ({i['title']})")


def main():
    day = build.latest_run_date()
    if not day:
        print("No raw data folders found under data/raw", file=sys.stderr)
        return 1
    with open(status_mod.STATUS_JSON, encoding="utf-8") as f:
        status = json.load(f)
    rows = build.read_history()
    new, removed = status_mod.todays_changes(rows, day)
    body = make_body(day, new, removed, status)

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print(f"[dry run: GITHUB_TOKEN / GITHUB_REPOSITORY not set] issue title: {day}\n")
        print(body)
        return 0
    publish(repo, token, day, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
