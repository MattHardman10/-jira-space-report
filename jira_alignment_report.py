"""
Jira Strategic Alignment Report (REST Super) - monthly

Answers: is our Change and Transform work (Programs, Projects, Epics) aligned
to Big Bets and Key Member Outcomes (KMOs)? Shows the full picture - from all
active work, to work tagged with a Type of work, to aligned work - for every
division, and separates "fields not on screen" (config gap) from "fields
available but not used" (education gap).

Reuses helpers from jira_space_report.py (same repo) and the same secrets:
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, CONFLUENCE_PARENT_PAGE_ID
Optional:
  DRY_RUN=1  -> writes preview_alignment.html + PNGs/CSVs locally
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
    create_page, update_page, macro, status, section, kpi, table, img,
    page_link, jira_link, slug, _style, _save,
)

# ---------- Settings ----------
PAGE_TITLE = "Strategic alignment – Jira"
ISSUE_TYPES = ["Program", "Project", "Epic"]
ACTIVE_SINCE = "2026-07-01"          # active work = created or updated since this date (FY27)
FIELDS_LIVE = "September 2026"       # when Big Bets / KMOs / Type of work went live (AWG-84)
CHANGE_WORK = ["Change", "Transform"]
MIN_SAMPLE = 5                       # fewer measured items than this = "low sample"

FIELD_BIG_BETS = ["Big Bets"]
FIELD_KMOS = ["Key Member Outcomes (KMOs)", "Key Member Outcomes"]
FIELD_TYPE_OF_WORK = ["Type of work"]
FIELD_MOS = ["Measure of Success (MoS)"]           # optional, shown for information

HISTORY_FILE = "jar_history.csv"
HISTORY_COLS = ["month", "active", "tow_set", "items", "fully", "bigbet", "kmo"]

ALL_DIVS = DIVISIONS + [UNCATEGORISED]
ALIGN_ORDER = ["Fully aligned", "Big Bet only", "KMO only", "Not aligned"]
ALIGN_COLOURS = ["#22A06B", "#0C66E4", "#8F7EE7", "#C9372C"]
ADOPT_ORDER = ["Change or Transform", "Run", "Not set – fields available",
               "Not set – fields not on screen", "Not set – not checked"]
ADOPT_COLOURS = ["#22A06B", "#8590A2", "#E2B203", "#C9372C", "#DCDFE4"]
FIELD_STATUS = {"available": ("Available", "Green"), "partial": ("Partly available", "Yellow"),
                "missing": ("Not on screen", "Red"), "unknown": ("Not checked", "Grey")}


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


def _quiet_get(path, params=None):
    r = requests.get(f"{SITE}{path}", auth=AUTH, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def field_availability(space_keys, needed):
    """Checks each space's create screens for the in-scope issue types."""
    out = {}
    for key in sorted(space_keys):
        try:
            d = _quiet_get(f"/rest/api/3/issue/createmeta/{key}/issuetypes", {"maxResults": 100})
            types = [t for t in d.get("issueTypes", d.get("values", d.get("results", [])))
                     if t.get("name") in ISSUE_TYPES]
            if not types:
                out[key] = "unknown"
                continue
            found = set()
            for t in types:
                start = 0
                while True:
                    d = _quiet_get(f"/rest/api/3/issue/createmeta/{key}/issuetypes/{t['id']}",
                                   {"startAt": start, "maxResults": 200})
                    fl = d.get("fields", d.get("values", d.get("results", [])))
                    found |= {f.get("fieldId") or f.get("key") for f in fl}
                    start += len(fl)
                    if not fl or start >= d.get("total", 0):
                        break
            hits = needed & found
            out[key] = "available" if hits == needed else ("partial" if hits else "missing")
        except requests.HTTPError:
            out[key] = "unknown"
    return out


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
        if not space:                        # team-managed or template space
            continue
        bb, kmo = vals(f.get(f_bb)), vals(f.get(f_kmo))
        mos = vals(f.get(f_mos)) if f_mos else []
        tow = (vals(f.get(f_tow)) or ["Not set"])[0]
        align = ("Fully aligned" if bb and kmo else "Big Bet only" if bb
                 else "KMO only" if kmo else "Not aligned")
        rows.append({"key": i["key"], "summary": f.get("summary", ""),
                     "itype": f["issuetype"]["name"],
                     "status": (f.get("status") or {}).get("name", ""),
                     "space": space["key"], "space_name": space["name"],
                     "division": space["division"], "bb": bb, "kmo": kmo, "mos": mos,
                     "tow": tow, "align": align})
    return rows


def pct(n, d):
    return n / d if d else 0


def adopt_bucket(r, avail):
    if r["tow"] in CHANGE_WORK:
        return "Change or Transform"
    if r["tow"] != "Not set":
        return "Run"
    a = avail.get(r["space"], "unknown")
    if a == "available":
        return "Not set – fields available"
    if a in ("missing", "partial"):
        return "Not set – fields not on screen"
    return "Not set – not checked"


def missing_for(r):
    if r["tow"] == "Not set":
        return ["Type of work"]
    if r["tow"] in CHANGE_WORK:
        return [m for m, ok in (("Big Bet", r["bb"]), ("KMO", r["kmo"])) if not ok]
    return []


# ---------- Charts ----------
def chart_funnel(stages, filename):
    labels = [s[0] for s in stages][::-1]
    values = [s[1] for s in stages][::-1]
    notes = [s[2] for s in stages][::-1]
    colours = ["#22A06B", "#8F7EE7", "#0C66E4", "#1D7F8C", "#E2B203", "#626F86"][:len(stages)]
    fig, ax = plt.subplots(figsize=(10, 0.55 * len(stages) + 0.8))
    ypos = list(range(len(stages)))
    ax.barh(ypos, values, color=colours, height=0.62)
    peak = max(values + [1])
    for i, (v, n) in enumerate(zip(values, notes)):
        ax.text(v + peak * 0.01, i, f"{v:,}   {n}", va="center", fontsize=10, fontweight="bold")
    _style(ax)
    ax.set_yticks(ypos, labels)
    ax.set_xlim(0, peak * 1.35)
    ax.set_xlabel("Work items")
    return _save(fig, filename)


def _stacked_pct(ax, rows, order, colours, label_min=0.08, low_alpha=None):
    """rows = [(label, total, Counter)] - draws 100% stacked bars in fixed order."""
    ypos = list(range(len(rows)))[::-1]          # first row at top
    left = [0.0] * len(rows)
    for key, colour in zip(order, colours):
        shares = [pct(c[key], n) for _, n, c in rows]
        alphas = [low_alpha(n) if low_alpha else 1 for _, n, _ in rows]
        for i, (s, l, a) in enumerate(zip(shares, left, alphas)):
            if s:
                ax.barh(ypos[i], s, left=l, color=colour, height=0.64, alpha=a,
                        edgecolor="white", linewidth=1.2)
                if s >= label_min:
                    ax.text(l + s / 2, ypos[i], f"{s:.0%}", ha="center", va="center",
                            color=TEXT if (colour in ("#E2B203", "#DCDFE4") or a < 1) else "white",
                            fontsize=9, fontweight="bold")
        left = [x + y for x, y in zip(left, shares)]
    ax.set_yticks(ypos, [r[0] for r in rows])
    ax.set_xlim(0, 1.3)
    ax.set_xticks([0, .25, .5, .75, 1])
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    return ypos


def chart_adoption(rows, avail, filename):
    data = []
    for d in ALL_DIVS:
        items = [r for r in rows if r["division"] == d]
        data.append((d, len(items), Counter(adopt_bucket(r, avail) for r in items)))
    order = [o for o in ADOPT_ORDER if any(c[o] for _, _, c in data)] or ADOPT_ORDER[:1]
    colours = [ADOPT_COLOURS[ADOPT_ORDER.index(o)] for o in order]
    fig, ax = plt.subplots(figsize=(10, 0.5 * len(data) + 1.2))
    ypos = _stacked_pct(ax, data, order, colours)
    for y, (_, n, _) in zip(ypos, data):
        ax.text(1.01, y, f"{n:,} items" if n else "No active work", va="center", fontsize=9, color=MUTED)
    _style(ax)
    ax.set_xticks([0, .25, .5, .75, 1])
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c) for c in colours], labels=order,
              ncol=3, loc="lower center", bbox_to_anchor=(0.45, 1.0), frameon=False, fontsize=9)
    return _save(fig, filename)


def chart_alignment(ct, filename):
    data = []
    for d in ALL_DIVS:
        items = [r for r in ct if r["division"] == d]
        data.append((d, len(items), Counter(r["align"] for r in items)))
    fig, ax = plt.subplots(figsize=(10, 0.5 * len(data) + 1.2))
    ypos = _stacked_pct(ax, data, ALIGN_ORDER, ALIGN_COLOURS,
                        low_alpha=lambda n: 0.4 if 0 < n < MIN_SAMPLE else 1)
    for y, (_, n, _) in zip(ypos, data):
        if not n:
            ax.text(0.01, y, "No change or transform work tagged yet", va="center",
                    fontsize=9, color=MUTED, style="italic")
        else:
            note = f"{n} item{'s' if n != 1 else ''}" + (" · low sample" if n < MIN_SAMPLE else "")
            ax.text(1.01, y, note, va="center", fontsize=9, color=MUTED)
    _style(ax)
    ax.set_xticks([0, .25, .5, .75, 1])
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.45, 1.0), frameon=False,
              handles=[plt.Rectangle((0, 0), 1, 1, color=c) for c in ALIGN_COLOURS], labels=ALIGN_ORDER)
    return _save(fig, filename)


def chart_heatmap(ct, bb_options, filename):
    bbs = bb_options + sorted({b for r in ct for b in r["bb"]} - set(bb_options))
    grid = [[sum(1 for r in ct if r["division"] == d and b in r["bb"]) for d in ALL_DIVS] for b in bbs]
    peak = max([max(r) for r in grid] + [1])
    fig, ax = plt.subplots(figsize=(11, 0.55 * max(len(bbs), 1) + 2.2))
    ax.imshow(grid, cmap="Blues", aspect="auto", vmin=0, vmax=peak * 1.15)
    for y, row in enumerate(grid):
        for x, v in enumerate(row):
            ax.text(x, y, str(v) if v else "–", ha="center", va="center", fontsize=10,
                    fontweight="bold" if v else "normal",
                    color="white" if v > peak * 0.55 else (TEXT if v else MUTED))
    ax.set_yticks(range(len(bbs)), [f"{textwrap.shorten(b, 45)}  ({sum(r)})" for b, r in zip(bbs, grid)])
    ax.set_xticks(range(len(ALL_DIVS)), [textwrap.fill(d, 13) for d in ALL_DIVS], fontsize=8.5)
    ax.xaxis.tick_top()
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([x - 0.5 for x in range(1, len(ALL_DIVS))], minor=True)
    ax.set_yticks([y - 0.5 for y in range(1, len(bbs))], minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    return _save(fig, filename)


def chart_values(counter, options, colour, filename):
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
    ax.set_xlabel("Change & transform items")
    return _save(fig, filename)


def chart_trend(history, filename):
    months = [h["month"] for h in history]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for col, label, colour in (("tow_set", "Type of work set", "#626F86"),
                               ("fully", "Fully aligned", "#22A06B"),
                               ("bigbet", "With a Big Bet", "#0C66E4"),
                               ("kmo", "With a KMO", "#8F7EE7")):
        pts = [(m, float(h[col])) for m, h in zip(months, history) if h.get(col) not in (None, "")]
        if not pts:
            continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", linewidth=2.2,
                color=colour, label=label)
        ax.annotate(f"{pts[-1][1]:.0%}", pts[-1], textcoords="offset points", xytext=(8, 0),
                    va="center", fontsize=9, fontweight="bold", color=colour)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.tick_params(colors=TEXT, length=0)
    ax.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, filename)


# ---------- Files (attachments) ----------
def load_history(pid):
    try:
        res = api("GET", f"/wiki/rest/api/content/{pid}/child/attachment",
                  params={"filename": HISTORY_FILE}).json().get("results", [])
        if not res:
            return []
        r = requests.get(f"{SITE}/wiki{res[0]['_links']['download']}", auth=AUTH, timeout=60)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except requests.HTTPError:
        return []


def save_history(history, month, stats):
    history = [h for h in history if h.get("month") != month]
    history.append({"month": month, **{k: stats[k] for k in HISTORY_COLS[1:]}})
    history.sort(key=lambda h: datetime.strptime(h["month"], "%b %Y"))
    with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(history)
    return history


def write_fix_csv(items, avail, filename):
    with open(filename, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Key", "Link", "Summary", "Work type", "Status", "Space key", "Space",
                    "Fields on screen", "Type of work", "Big Bets", "KMOs", "Missing"])
        for r in items:
            w.writerow([r["key"], f"{SITE}/browse/{r['key']}", r["summary"], r["itype"], r["status"],
                        r["space"], r["space_name"], FIELD_STATUS[avail.get(r["space"], "unknown")][0],
                        r["tow"], "; ".join(r["bb"]), "; ".join(r["kmo"]), ", ".join(missing_for(r))])
    return filename


def upload_file(pid, path, mime):
    with open(path, "rb") as f:
        api("PUT", f"/wiki/rest/api/content/{pid}/child/attachment",
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (path, f, mime)}, data={"minorEdit": "true"})


def attachment_link(filename, text):
    return (f'<ac:link><ri:attachment ri:filename="{e(filename)}"/>'
            f"<ac:plain-text-link-body><![CDATA[{text}]]></ac:plain-text-link-body></ac:link>")


# ---------- Page ----------
def division_cell(d):
    return page_link(CHILD_TITLE.format(division=d), d) if d in DIVISIONS else e(d)


def compute_stats(rows):
    ct = [r for r in rows if r["tow"] in CHANGE_WORK]
    n = len(ct)
    return {"active": len(rows),
            "tow_set": f"{pct(sum(1 for r in rows if r['tow'] != 'Not set'), len(rows)):.4f}",
            "items": n,
            "fully": f"{pct(sum(1 for r in ct if r['align'] == 'Fully aligned'), n):.4f}",
            "bigbet": f"{pct(sum(1 for r in ct if r['bb']), n):.4f}",
            "kmo": f"{pct(sum(1 for r in ct if r['kmo']), n):.4f}"}


def build_page(rows, avail, bb_options, kmo_options, mos_options, history, now):
    active = len(rows)
    tow_set = [r for r in rows if r["tow"] != "Not set"]
    ct = [r for r in rows if r["tow"] in CHANGE_WORK]
    n = len(ct)
    c_all = Counter(r["align"] for r in ct)
    n_bb = sum(1 for r in ct if r["bb"])
    n_kmo = sum(1 for r in ct if r["kmo"])
    n_full = c_all["Fully aligned"]

    space_items = defaultdict(list)
    for r in rows:
        space_items[r["space"]].append(r)
    sp_missing = sorted(k for k in space_items if avail.get(k) in ("missing", "partial"))
    sp_unused = sorted(k for k, items in space_items.items() if avail.get(k) == "available"
                       and not any(i["bb"] or i["kmo"] or i["tow"] != "Not set" for i in items))
    sp_unknown = sorted(k for k in space_items if avail.get(k, "unknown") == "unknown")

    # Charts
    funnel = chart_funnel([
        ("Active work items", active, ""),
        ("Type of work set", len(tow_set), f"({pct(len(tow_set), active):.0%} of active)"),
        ("Change or Transform", n, f"({pct(n, active):.0%} of active)"),
        ("With a Big Bet", n_bb, f"({pct(n_bb, n):.0%} of change & transform)"),
        ("With a KMO", n_kmo, f"({pct(n_kmo, n):.0%} of change & transform)"),
        ("Fully aligned", n_full, f"({pct(n_full, n):.0%} of change & transform)"),
    ], "jar_funnel.png")
    charts = [funnel,
              chart_adoption(rows, avail, "jar_adoption.png"),
              chart_alignment(ct, "jar_alignment.png"),
              chart_heatmap(ct, bb_options, "jar_heatmap.png"),
              chart_values(Counter(b for r in ct for b in r["bb"]), bb_options, "#0C66E4", "jar_bigbets.png"),
              chart_values(Counter(k for r in ct for k in r["kmo"]), kmo_options, "#8F7EE7", "jar_kmos.png")]
    mos_chart = (chart_values(Counter(m for r in ct for m in r["mos"]), mos_options, "#1D7F8C", "jar_mos.png")
                 if mos_options is not None else None)
    trend_chart = chart_trend(history, "jar_trend.png") if len(history) > 1 else None
    charts += [c for c in (mos_chart, trend_chart) if c]

    since = datetime.strptime(ACTIVE_SINCE, "%Y-%m-%d").strftime("%d %B %Y")
    banner = macro("info", None,
                   f"<p>Monthly view of whether Change and Transform work is aligned to "
                   f"<strong>Big Bets</strong> and <strong>Key Member Outcomes (KMOs)</strong>. "
                   f"Covers {', '.join(ISSUE_TYPES)} work items in company-managed spaces, created or "
                   f"updated since {since}. Last refreshed <strong>{now}</strong> (Sydney). "
                   f"Do not edit this page.</p>")
    context = macro("note", {"title": "Reading this report"},
                    f"<p>The Big Bets, KMOs and Type of work fields went live in <strong>{FIELDS_LIVE}</strong>. "
                    f"Most existing work hasn't been tagged yet, so start with <strong>Coverage</strong> - "
                    f"how much work can be measured - before reading the alignment percentages. "
                    f"Divisions with fewer than {MIN_SAMPLE} measured items are marked <em>low sample</em>.</p>")
    method = macro("expand", {"title": "How this is measured"},
                   "<p><strong>Active work</strong> = Programs, Projects and Epics created or updated since "
                   f"{since}.</p>"
                   f"<p><strong>Measured work</strong> = active work with Type of work = {' or '.join(CHANGE_WORK)}. "
                   "Run work is excluded from alignment.</p>"
                   "<p><strong>Fully aligned</strong> = at least one Big Bet and at least one KMO. "
                   "<strong>Big Bet only / KMO only</strong> = one of the two. <strong>Not aligned</strong> = neither.</p>"
                   "<p><strong>Fields on screen</strong> is checked from each space's create screen for these work types. "
                   "<em>Not on screen</em> means the space needs a configuration change before teams can use the fields; "
                   "<em>not checked</em> means the report account couldn't read that space's create screen.</p>"
                   "<p>Division comes from each space's Division | Type category; unmapped spaces appear as Uncategorised.</p>")

    kpi_cov = section("three_equal",
                      kpi("Active work items", f"{active:,}", f"{', '.join(ISSUE_TYPES)} since {since}", "#F7F8F9"),
                      kpi("Type of work set", f"{pct(len(tow_set), active):.0%}",
                          f"{len(tow_set):,} of {active:,} active items", "#FFF7D6"),
                      kpi("Change & transform items", n, "Tagged Change or Transform - measured below", "#E9F2FF"))
    kpi_align = section("three_equal",
                        kpi("Fully aligned", f"{pct(n_full, n):.0%}", f"{n_full} of {n} change & transform items", "#DCFFF1"),
                        kpi("With a Big Bet", f"{pct(n_bb, n):.0%}", f"{n_bb} of {n}", "#E9F2FF"),
                        kpi("With a KMO", f"{pct(n_kmo, n):.0%}", f"{n_kmo} of {n}", "#F3F0FF"))
    kpi_spaces = section("three_equal",
                         kpi("Spaces with active work", len(space_items), "Company-managed", "#F7F8F9"),
                         kpi("Fields not on screen", len(sp_missing), "Configuration change needed", "#FFECEB"),
                         kpi("Fields available, not used", len(sp_unused), "Education needed", "#FFF7D6"))

    # Division table - every division, always
    div_rows = []
    for d in ALL_DIVS:
        items = [r for r in rows if r["division"] == d]
        dct = [r for r in items if r["tow"] in CHANGE_WORK]
        c = Counter(r["align"] for r in dct)
        nset = sum(1 for r in items if r["tow"] != "Not set")
        if not dct:
            score = "–"
        else:
            score = f"<strong>{pct(c['Fully aligned'], len(dct)):.0%}</strong>"
            if len(dct) < MIN_SAMPLE:
                score += " " + status("Low sample", "Grey")
        div_rows.append([division_cell(d), f"{len(items):,}",
                         f"{pct(nset, len(items)):.0%}" if items else "–",
                         len(dct), c["Fully aligned"], c["Big Bet only"], c["KMO only"], c["Not aligned"], score])
    div_table = table(["Division", "Active items", "Type of work set", "Change & transform",
                       "Fully aligned", "Big Bet only", "KMO only", "Not aligned", "% fully aligned"],
                      div_rows, numeric_cols=range(1, 9))

    # Space table
    sp_rows = []
    for k, items in sorted(space_items.items(), key=lambda kv: (ALL_DIVS.index(kv[1][0]["division"]), kv[0])):
        sct = [i for i in items if i["tow"] in CHANGE_WORK]
        fa = sum(1 for i in sct if i["align"] == "Fully aligned")
        label, colour = FIELD_STATUS[avail.get(k, "unknown")]
        sp_rows.append([jira_link(k), e(items[0]["space_name"]), division_cell(items[0]["division"]),
                        status(label, colour), len(items),
                        f"{pct(sum(1 for i in items if i['tow'] != 'Not set'), len(items)):.0%}",
                        len(sct), f"{pct(fa, len(sct)):.0%}" if sct else "–"])
    space_table = macro("expand", {"title": f"Show all {len(sp_rows)} spaces with active work"},
                        table(["Key", "Space", "Division", "Fields on screen", "Active items",
                               "Type of work set", "Change & transform", "% fully aligned"],
                              sp_rows, numeric_cols=(4, 5, 6, 7)))

    def key_list(keys):
        return ", ".join(jira_link(k) for k in keys) if keys else "None"

    space_lists = (f"<p><strong>Fields not on screen ({len(sp_missing)})</strong> - add the fields to these "
                   f"spaces' screens: {key_list(sp_missing)}</p>"
                   f"<p><strong>Fields available but not used ({len(sp_unused)})</strong> - education needed: "
                   f"{key_list(sp_unused)}</p>"
                   + (f"<p><strong>Not checked ({len(sp_unknown)})</strong> - report account can't read the "
                      f"create screen: {key_list(sp_unknown)}</p>" if sp_unknown else ""))

    # Items to fix - summary per division and space, full list as CSV
    to_fix = [r for r in rows if missing_for(r)]
    csv_files, fix_html = [], ""
    for d in ALL_DIVS:
        items = sorted((r for r in to_fix if r["division"] == d), key=lambda r: (r["space"], r["key"]))
        if not items:
            fix_html += f"<p><strong>{e(d)}</strong> - nothing to fix.</p>"
            continue
        fn = write_fix_csv(items, avail, f"jar_fix_{slug(d)}.csv")
        csv_files.append(fn)
        per_space = defaultdict(Counter)
        for r in items:
            for m in missing_for(r):
                per_space[r["space"]][m] += 1
        names = {r["space"]: r["space_name"] for r in items}
        srows = [[jira_link(k), e(names[k]), status(*FIELD_STATUS[avail.get(k, "unknown")]),
                  c["Type of work"], c["Big Bet"], c["KMO"], sum(c.values())]
                 for k, c in sorted(per_space.items(), key=lambda kv: -sum(kv[1].values()))]
        fix_html += macro("expand", {"title": f"{d} - {len(items):,} items to fix"},
                          f"<p>{attachment_link(fn, 'Download the full list (CSV)')} - send this to the division.</p>"
                          + table(["Key", "Space", "Fields on screen", "Missing Type of work",
                                   "Missing Big Bet", "Missing KMO", "Total"], srows, numeric_cols=(3, 4, 5, 6)))

    trend = (img(trend_chart, 960) if trend_chart else
             "<p><em>The trend builds from the first monthly run - a line appears from the second month.</em></p>")

    body = (
        "<ac:layout>"
        + section("single", banner + context + method)
        + section("single", "<h2>The full picture</h2>"
                  "<p>From all active work, to work tagged with a Type of work, to work aligned to strategy.</p>"
                  + img(charts[0], 960))
        + section("single", "<h2>1. Coverage - can we measure it?</h2>")
        + kpi_cov
        + section("single", "<h3>Type of work by division</h3>"
                  "<p>Share of each division's active work by Type of work. Yellow is work in spaces that have the "
                  "fields but haven't used them; red is work in spaces where the fields aren't on screen yet.</p>"
                  + img(charts[1], 960))
        + section("single", "<h2>2. Alignment - is change work linked to strategy?</h2>")
        + kpi_align
        + section("single", "<h3>Alignment by division</h3>"
                  f"<p>Faded bars have fewer than {MIN_SAMPLE} measured items.</p>" + img(charts[2], 960) + div_table)
        + section("single", "<h3>Big Bets by division</h3>"
                  "<p>Change and transform items linked to each Big Bet. A row of dashes means no work is linked.</p>"
                  + img(charts[3], 960))
        + section("two_equal", "<h3>Big Bets in use</h3>" + img(charts[4], 460),
                  "<h3>KMOs in use</h3>" + img(charts[5], 460))
        + (section("single", "<h3>Measures of Success in use</h3>"
                   "<p>For information - not part of the alignment score.</p>" + img(mos_chart, 960))
           if mos_chart else "")
        + section("single", "<h2>3. Spaces</h2>" + space_lists + space_table)
        + section("single", "<h2>4. Trend</h2>" + trend)
        + section("single", "<h2>5. Items to fix</h2>"
                  "<p>Active work with no Type of work, and change or transform work missing a Big Bet or KMO. "
                  "Each division's full list is attached as a CSV.</p>" + fix_html)
        + "</ac:layout>"
    )
    return body, charts, csv_files


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

    existing = {t["name"] for t in api("GET", "/rest/api/3/issuetype").json()}
    types = [t for t in ISSUE_TYPES if t in existing]
    if set(ISSUE_TYPES) - set(types):
        print(f"Warning: issue types not found and skipped: {set(ISSUE_TYPES) - set(types)}")
    jql = (f"issuetype in ({', '.join(chr(34) + t + chr(34) for t in types)}) "
           f'AND (created >= "{ACTIVE_SINCE}" OR updated >= "{ACTIVE_SINCE}")')
    issues = search_items(jql, ["summary", "status", "project", "issuetype", f_bb, f_kmo, f_tow]
                          + ([f_mos] if f_mos else []))
    rows = analyse(issues, f_bb, f_kmo, f_tow, spaces, f_mos)
    print(f"{len(issues)} active items found, {len(rows)} in company-managed spaces")

    avail = field_availability({r["space"] for r in rows}, {f_bb, f_kmo, f_tow})
    print("Field availability:", dict(Counter(avail.values())))

    bb_options, kmo_options = field_options(f_bb), field_options(f_kmo)
    mos_options = field_options(f_mos) if f_mos else None
    stats = compute_stats(rows)

    if DRY_RUN:
        body, _, _ = build_page(rows, avail, bb_options, kmo_options, mos_options, [], now)
        open("preview_alignment.html", "w", encoding="utf-8").write(body)
        raise SystemExit(f"DRY RUN: preview written. {stats}")

    parent = get_page(PAGE_ID)
    pid = child_pages(PAGE_ID).get(PAGE_TITLE) or create_page(parent["spaceId"], PAGE_ID, PAGE_TITLE)
    history = save_history(load_history(pid), month, stats)
    body, charts, csvs = build_page(rows, avail, bb_options, kmo_options, mos_options, history, now)

    for path in charts:
        upload_file(pid, path, "image/png")
    for path in csvs + [HISTORY_FILE]:
        upload_file(pid, path, "text/csv")
    update_page(pid, body)
    print(f"Updated '{PAGE_TITLE}': {stats}")
