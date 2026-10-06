"""
Jira Strategic Alignment Report (REST Super) - monthly

Measures whether Change and Transform work (Programs, Projects, Epics) is
aligned to Big Bets and Key Member Outcomes (KMOs), by division and space.
Publishes to a child page of the Jira space portfolio page and keeps a
monthly history (CSV attachment) for the trend chart.

Reuses helpers from jira_space_report.py (same repo) and the same secrets:
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, CONFLUENCE_PARENT_PAGE_ID
Optional:
  DRY_RUN=1  -> writes preview_alignment.html + PNGs locally
"""
import csv
import io
import textwrap
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

from jira_space_report import (
    SITE, AUTH, PAGE_ID, DRY_RUN, DIVISIONS, UNCATEGORISED, CHILD_TITLE,
    TEXT, MUTED, GRID, e, api, get_projects, classify, get_page, child_pages,
    create_page, update_page, macro, section, kpi, table, img, page_link,
    jira_link, _style, _save,
)

# ---------- Settings ----------
PAGE_TITLE = "Strategic alignment – Jira"
ISSUE_TYPES = ["Program", "Project", "Epic"]
FY_START = "2026-07-01"                    # include work completed since this date
CHANGE_WORK = ["Change", "Transform"]      # Type of work values measured for alignment
# Field names (first match wins)
FIELD_BIG_BETS = ["Big Bets"]
FIELD_KMOS = ["Key Member Outcomes (KMOs)", "Key Member Outcomes"]
FIELD_TYPE_OF_WORK = ["Type of work"]
FIELD_MOS = ["Measure of Success (MoS)"]       # optional - shown as "in use" only
HISTORY_FILE = "jar_history.csv"

ALIGN_ORDER = ["Fully aligned", "Big Bet only", "KMO only", "Not aligned"]
ALIGN_COLOURS = ["#22A06B", "#0C66E4", "#8F7EE7", "#C9372C"]


# ---------- Jira ----------
def find_field(fields, names, required=True):
    for name in names:
        exact = [f for f in fields if f["name"].strip().lower() == name.lower()]
        if exact:
            if len(exact) > 1:
                print(f"Warning: {len(exact)} fields named '{name}', using {exact[0]['id']}")
            print(f"Field '{name}' -> {exact[0]['id']}")
            return exact[0]["id"]
    similar = [f["name"] for f in fields if names[0].split()[0].lower() in f["name"].lower()][:10]
    if required:
        raise SystemExit(f"Field not found: {names}. Similar names: {similar}")
    print(f"Optional field not found: {names}")
    return None


def field_options(fid):
    """All enabled option values for a select field, so unused values show as 0."""
    opts = []
    try:
        for ctx in api("GET", f"/rest/api/3/field/{fid}/context").json().get("values", []):
            start = 0
            while True:
                d = api("GET", f"/rest/api/3/field/{fid}/context/{ctx['id']}/option",
                        params={"startAt": start, "maxResults": 100}).json()
                for o in d.get("values", []):
                    if not o.get("disabled") and o["value"] not in opts:
                        opts.append(o["value"])
                if d.get("isLast", True) or not d.get("values"):
                    break
                start += len(d["values"])
    except requests.HTTPError:
        print(f"Could not read options for {fid}; using values found in data")
    return opts


def search_items(jql, field_ids):
    items, token = [], None
    while True:
        body = {"jql": jql, "fields": field_ids, "maxResults": 100}
        if token:
            body["nextPageToken"] = token
        data = api("POST", "/rest/api/3/search/jql", json=body).json()
        items += data.get("issues", [])
        token = data.get("nextPageToken")
        if not token or data.get("isLast"):
            return items


def vals(v):
    if not v:
        return []
    if isinstance(v, dict):
        v = [v]
    return [x.get("value") if isinstance(x, dict) else str(x) for x in v if x]


# ---------- Analysis ----------
def analyse(issues, f_bb, f_kmo, f_tow, spaces, f_mos=None):
    by_key = {s["key"]: s for s in spaces}
    rows = []
    for i in issues:
        f = i["fields"]
        space = by_key.get(f["project"]["key"])
        if not space:                       # team-managed or template space
            continue
        bb, kmo = vals(f.get(f_bb)), vals(f.get(f_kmo))
        mos = vals(f.get(f_mos)) if f_mos else []
        tow = (vals(f.get(f_tow)) or ["Not set"])[0]
        align = ("Fully aligned" if bb and kmo else "Big Bet only" if bb
                 else "KMO only" if kmo else "Not aligned")
        rows.append({"key": i["key"], "summary": f.get("summary", ""),
                     "itype": f["issuetype"]["name"], "space": space["key"],
                     "space_name": space["name"], "division": space["division"],
                     "bb": bb, "kmo": kmo, "mos": mos, "tow": tow, "align": align})
    return rows


def pct(n, d):
    return n / d if d else 0


def division_rows(ct):
    divs = [d for d in DIVISIONS + [UNCATEGORISED] if any(r["division"] == d for r in ct)]
    out = []
    for d in divs:
        c = Counter(r["align"] for r in ct if r["division"] == d)
        out.append((d, sum(c.values()), c))
    return out


# ---------- Charts ----------
def chart_alignment_by_division(ct, filename):
    data = sorted(division_rows(ct), key=lambda x: pct(x[2]["Fully aligned"], x[1]))
    fig, ax = plt.subplots(figsize=(10, 0.5 * max(len(data), 1) + 1))
    ypos = list(range(len(data)))
    left = [0.0] * len(data)
    for a, colour in zip(ALIGN_ORDER, ALIGN_COLOURS):
        shares = [pct(c[a], n) for _, n, c in data]
        ax.barh(ypos, shares, left=left, color=colour, label=a, height=0.64,
                edgecolor="white", linewidth=1.2)
        for i, (s, l) in enumerate(zip(shares, left)):
            if s >= 0.08:
                ax.text(l + s / 2, i, f"{s:.0%}", ha="center", va="center",
                        color="white", fontsize=9, fontweight="bold")
        left = [x + y for x, y in zip(left, shares)]
    for i, (_, n, _) in enumerate(data):
        ax.text(1.01, i, f"{n} items", va="center", fontsize=9, color=MUTED)
    _style(ax)
    ax.set_yticks(ypos, [d for d, _, _ in data])
    ax.set_xlim(0, 1.12)
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_xticks([0, .25, .5, .75, 1])
    ax.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, filename)


def chart_heatmap(ct, bb_options, filename):
    bbs = bb_options + sorted({b for r in ct for b in r["bb"]} - set(bb_options))
    divs = [d for d in DIVISIONS + [UNCATEGORISED] if any(r["division"] == d for r in ct)]
    grid = [[sum(1 for r in ct if r["division"] == d and b in r["bb"]) for d in divs] for b in bbs]
    fig, ax = plt.subplots(figsize=(10, 0.55 * max(len(bbs), 1) + 2.2))
    ax.imshow(grid, cmap="Blues", aspect="auto", vmin=0, vmax=max([max(r) for r in grid] + [1]) * 1.15)
    peak = max([max(r) for r in grid] + [1])
    for y, row in enumerate(grid):
        for x, v in enumerate(row):
            ax.text(x, y, str(v) if v else "–", ha="center", va="center", fontsize=10,
                    fontweight="bold" if v else "normal",
                    color="white" if v > peak * 0.55 else (TEXT if v else MUTED))
    totals = [sum(r) for r in grid]
    ax.set_yticks(range(len(bbs)), [f"{textwrap.shorten(b, 45)}  ({t})" for b, t in zip(bbs, totals)])
    ax.set_xticks(range(len(divs)), [textwrap.fill(d, 14) for d in divs], fontsize=9)
    ax.xaxis.tick_top()
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([x - 0.5 for x in range(1, len(divs))], minor=True)
    ax.set_yticks([y - 0.5 for y in range(1, len(bbs))], minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    return _save(fig, filename)


def chart_values(counter, options, colour, xlabel, filename):
    labels = options + sorted(set(counter) - set(options))
    labels = sorted(labels, key=lambda l: counter.get(l, 0))
    values = [counter.get(l, 0) for l in labels]
    fig, ax = plt.subplots(figsize=(11, max(2.4, 0.42 * len(labels) + 1)))
    ypos = list(range(len(labels)))
    ax.barh(ypos, values, color=colour, height=0.62)
    peak = max(values + [1])
    for i, v in enumerate(values):
        ax.text(v + peak * 0.01, i, str(v) if v else "0 - no work linked", va="center",
                fontsize=9, fontweight="bold" if v else "normal",
                color=TEXT if v else "#C9372C")
    _style(ax)
    ax.set_yticks(ypos, [textwrap.shorten(l, 70) for l in labels])
    ax.set_xlim(0, peak * 1.25)
    ax.set_xlabel(xlabel)
    return _save(fig, filename)


def chart_trend(history, filename):
    months = [h["month"] for h in history]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for col, label, colour in (("fully", "Fully aligned", "#22A06B"),
                               ("bigbet", "With a Big Bet", "#0C66E4"),
                               ("kmo", "With a KMO", "#8F7EE7")):
        ys = [float(h[col]) for h in history]
        ax.plot(months, ys, marker="o", linewidth=2.2, color=colour, label=label)
        ax.annotate(f"{ys[-1]:.0%}", (months[-1], ys[-1]), textcoords="offset points",
                    xytext=(8, 0), va="center", fontsize=9, fontweight="bold", color=colour)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.tick_params(colors=TEXT, length=0)
    ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, filename)


# ---------- History (CSV attachment on the report page) ----------
def load_history(pid):
    try:
        res = api("GET", f"/wiki/rest/api/content/{pid}/child/attachment",
                  params={"filename": HISTORY_FILE}).json().get("results", [])
        if not res:
            return []
        r = requests.get(f"{SITE}/wiki{res[0]['_links']['download']}",
                         auth=AUTH, timeout=60)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except requests.HTTPError:
        return []


def save_history(history, month, n, fully, bb, kmo):
    history = [h for h in history if h["month"] != month]
    history.append({"month": month, "items": n, "fully": f"{fully:.4f}",
                    "bigbet": f"{bb:.4f}", "kmo": f"{kmo:.4f}"})
    history.sort(key=lambda h: datetime.strptime(h["month"], "%b %Y"))
    with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["month", "items", "fully", "bigbet", "kmo"])
        w.writeheader()
        w.writerows(history)
    return history


def upload_file(pid, path, mime):
    with open(path, "rb") as f:
        api("PUT", f"/wiki/rest/api/content/{pid}/child/attachment",
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (path, f, mime)}, data={"minorEdit": "true"})


# ---------- Page ----------
def division_cell(d):
    return page_link(CHILD_TITLE.format(division=d), d) if d in DIVISIONS else e(d)


def build_page(rows, bb_options, kmo_options, history, now, mos_options=None):
    ct = [r for r in rows if r["tow"] in CHANGE_WORK]
    n = len(ct)
    c_all = Counter(r["align"] for r in ct)
    fully = pct(c_all["Fully aligned"], n)
    with_bb = pct(sum(1 for r in ct if r["bb"]), n)
    with_kmo = pct(sum(1 for r in ct if r["kmo"]), n)
    not_set = [r for r in rows if r["tow"] == "Not set"]

    spaces = defaultdict(list)
    for r in rows:
        spaces[r["space"]].append(r)
    no_data = sorted(k for k, items in spaces.items()
                     if not any(i["bb"] or i["kmo"] or i["tow"] != "Not set" for i in items))

    charts = [chart_alignment_by_division(ct, "jar_division.png"),
              chart_heatmap(ct, bb_options, "jar_heatmap.png"),
              chart_values(Counter(b for r in ct for b in r["bb"]), bb_options, "#0C66E4",
                           "Change & transform items", "jar_bigbets.png"),
              chart_values(Counter(k for r in ct for k in r["kmo"]), kmo_options, "#8F7EE7",
                           "Change & transform items", "jar_kmos.png")]
    trend_chart = chart_trend(history, "jar_trend.png") if history else None
    mos_chart = (chart_values(Counter(m for r in ct for m in r["mos"]), mos_options, "#1D7F8C",
                              "Change & transform items", "jar_mos.png")
                 if mos_options is not None else None)
    charts += [c for c in (trend_chart, mos_chart) if c]

    banner = macro("info", None,
                   f"<p>Monthly view of how well Change and Transform work is aligned to "
                   f"<strong>Big Bets</strong> and <strong>Key Member Outcomes (KMOs)</strong>. "
                   f"Covers {', '.join(ISSUE_TYPES)} work items in company-managed spaces that are open "
                   f"or were completed since {datetime.strptime(FY_START, '%Y-%m-%d'):%d %B %Y}. "
                   f"Last refreshed <strong>{now}</strong> (Sydney). Do not edit this page.</p>")
    method = macro("expand", {"title": "How this is measured"},
                   "<p><strong>Fully aligned</strong> = at least one Big Bet and at least one KMO selected. "
                   "<strong>Big Bet only / KMO only</strong> = one of the two. "
                   "<strong>Not aligned</strong> = neither.</p>"
                   f"<p>Only items with Type of work = {' or '.join(CHANGE_WORK)} are measured. "
                   "Run work is excluded. Items with no Type of work can't be measured and are listed "
                   "separately so they can be fixed.</p>"
                   "<p>Division comes from each space's Division | Type category.</p>")

    kpi1 = section("three_equal",
                   kpi("Fully aligned", f"{fully:.0%}", f"{c_all['Fully aligned']} of {n} change & transform items", "#DCFFF1"),
                   kpi("With a Big Bet", f"{with_bb:.0%}", "Linked to at least one Big Bet", "#E9F2FF"),
                   kpi("With a KMO", f"{with_kmo:.0%}", "Linked to at least one KMO", "#F3F0FF"))
    kpi2 = section("three_equal",
                   kpi("Items measured", n, f"{', '.join(CHANGE_WORK)} work", "#F7F8F9"),
                   kpi("Type of work not set", len(not_set), "Can't be measured until set", "#FFF7D6"),
                   kpi("Spaces with no alignment data", len(no_data), "None of the fields used", "#FFECEB"))

    div_table = table(
        ["Division", "Items", "Fully aligned", "Big Bet only", "KMO only", "Not aligned", "% fully aligned"],
        [[division_cell(d), t, c["Fully aligned"], c["Big Bet only"], c["KMO only"], c["Not aligned"],
          f"<strong>{pct(c['Fully aligned'], t):.0%}</strong>"] for d, t, c in division_rows(ct)],
        numeric_cols=range(1, 7))

    # Space table
    space_rows = []
    for k, items in sorted(spaces.items(), key=lambda kv: (kv[1][0]["division"], kv[0])):
        sct = [i for i in items if i["tow"] in CHANGE_WORK]
        fa = sum(1 for i in sct if i["align"] == "Fully aligned")
        space_rows.append([jira_link(k), e(items[0]["space_name"]), division_cell(items[0]["division"]),
                           len(sct), f"{pct(fa, len(sct)):.0%}" if sct else "–",
                           sum(1 for i in items if i["tow"] == "Not set")])
    space_table = macro("expand", {"title": f"Show alignment for all {len(space_rows)} spaces"},
                        table(["Key", "Space", "Division", "Change & transform items",
                               "% fully aligned", "Type of work not set"], space_rows,
                              numeric_cols=(3, 4, 5)))

    # Items to fix, grouped by division
    def missing(r):
        if r["tow"] == "Not set":
            return "Type of work"
        return ", ".join(m for m, ok in (("Big Bet", r["bb"]), ("KMO", r["kmo"])) if not ok)

    to_fix = [r for r in rows if r["tow"] == "Not set" or (r["tow"] in CHANGE_WORK and r["align"] != "Fully aligned")]
    fix_html = ""
    for d in DIVISIONS + [UNCATEGORISED]:
        items = sorted((r for r in to_fix if r["division"] == d), key=lambda r: (r["space"], r["key"]))
        if items:
            fix_html += macro("expand", {"title": f"{d} - {len(items)} items to fix"},
                              table(["Item", "Summary", "Type", "Space", "Missing"],
                                    [[jira_link(r["key"]), e(r["summary"]), e(r["itype"]),
                                      e(r["space"]), e(missing(r))] for r in items]))
    if not fix_html:
        fix_html = macro("tip", None, "<p>Every measured item is fully aligned.</p>")

    trend = (img(trend_chart, 960) if history and len(history) > 1 else
             "<p><em>The trend builds up from this month - a line appears after the second monthly run.</em></p>")

    body = (
        "<ac:layout>"
        + section("single", banner + method)
        + kpi1 + kpi2
        + section("single", "<h2>Alignment by division</h2>" + img(charts[0], 960) + div_table)
        + section("single", "<h2>Big Bets by division</h2>"
                  "<p>Change and transform items linked to each Big Bet, by division.</p>" + img(charts[1], 960))
        + section("single", "<h2>Big Bets in use</h2>" + img(charts[2], 960))
        + section("single", "<h2>KMOs in use</h2>" + img(charts[3], 960))
        + (section("single", "<h2>Measures of Success in use</h2>"
                   "<p>For information - not part of the alignment score.</p>" + img(mos_chart, 960))
           if mos_chart else "")
        + section("single", "<h2>Trend</h2>" + trend)
        + section("single", "<h2>Alignment by space</h2>" + space_table
                  + (f"<p><strong>Spaces with no alignment data:</strong> {', '.join(jira_link(k) for k in no_data)}</p>"
                     if no_data else ""))
        + section("single", "<h2>Items to fix</h2>"
                  "<p>Change and transform items missing a Big Bet or KMO, and items with no Type of work.</p>"
                  + fix_html)
        + "</ac:layout>"
    )
    return body, charts, (n, fully, with_bb, with_kmo)


# ---------- Main ----------
if __name__ == "__main__":
    now_dt = datetime.now(ZoneInfo("Australia/Sydney"))
    now, month = now_dt.strftime("%A %d %B %Y, %I:%M %p"), now_dt.strftime("%b %Y")

    projects = get_projects()
    if not projects:
        raise SystemExit("Jira returned 0 projects - check the email and API token secrets")
    _, spaces, _ = classify(projects)

    fields = api("GET", "/rest/api/3/field").json()
    f_bb, f_kmo, f_tow = (find_field(fields, n) for n in (FIELD_BIG_BETS, FIELD_KMOS, FIELD_TYPE_OF_WORK))
    f_mos = find_field(fields, FIELD_MOS, required=False)

    existing_types = {t["name"] for t in api("GET", "/rest/api/3/issuetype").json()}
    types = [t for t in ISSUE_TYPES if t in existing_types]
    missing_types = set(ISSUE_TYPES) - set(types)
    if missing_types:
        print(f"Warning: issue types not found and skipped: {missing_types}")
    type_list = ", ".join(f'"{t}"' for t in types)
    jql = (f"issuetype in ({type_list}) AND "
           f'(statusCategory != Done OR (statusCategory = Done AND updated >= "{FY_START}"))')
    issues = search_items(jql, ["summary", "project", "issuetype", f_bb, f_kmo, f_tow] + ([f_mos] if f_mos else []))
    rows = analyse(issues, f_bb, f_kmo, f_tow, spaces, f_mos)
    print(f"{len(issues)} items found, {len(rows)} in company-managed spaces")

    bb_options, kmo_options = field_options(f_bb), field_options(f_kmo)
    mos_options = field_options(f_mos) if f_mos else None

    if DRY_RUN:
        body, _, stats = build_page(rows, bb_options, kmo_options, [], now, mos_options)
        open("preview_alignment.html", "w", encoding="utf-8").write(body)
        raise SystemExit(f"DRY RUN: preview written. Items={stats[0]}, fully aligned={stats[1]:.0%}")

    parent = get_page(PAGE_ID)
    pid = child_pages(PAGE_ID).get(PAGE_TITLE) or create_page(parent["spaceId"], PAGE_ID, PAGE_TITLE)

    # First pass for this month's stats, then record history and build the final page
    _, _, stats = build_page(rows, bb_options, kmo_options, [], now, mos_options)
    history = save_history(load_history(pid), month, *stats)
    body, charts, _ = build_page(rows, bb_options, kmo_options, history, now, mos_options)

    for path in charts:
        upload_file(pid, path, "image/png")
    upload_file(pid, HISTORY_FILE, "text/csv")
    update_page(pid, body)
    print(f"Updated '{PAGE_TITLE}': {stats[0]} items, {stats[1]:.0%} fully aligned")
