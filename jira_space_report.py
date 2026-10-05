"""
Jira Space Report (REST Super)

Weekly refresh of a Confluence parent page plus one child page per division:
KPIs, PNG charts (uploaded as attachments), division summary with links to
division pages, space lead register and a "needs attention" list.

Env vars (GitHub Actions secrets):
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, CONFLUENCE_PARENT_PAGE_ID
Optional:
  DRY_RUN=1  -> writes preview HTML + PNGs locally instead of updating Confluence
"""
import os
import re
import html
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator

# ---------- Settings ----------
SITE = "https://restsuper.atlassian.net"
EMAIL = os.environ.get("ATLASSIAN_EMAIL", "").strip()
TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
PAGE_ID = os.environ.get("CONFLUENCE_PARENT_PAGE_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
INCLUDE_TEAM_MANAGED = False          # team-managed spaces are out of scope
TOP_LEADS = 15                        # leads shown in bar charts
CHILD_TITLE = "{division} – Jira spaces"

DIVISIONS = [
    "Data, Tech & Delivery",
    "Enterprise Risk",
    "Finance & Investment Operations",
    "Investments",
    "Member",
    "People & Culture",
    "Service",
    "Strategy & Corp Affairs",
]
TYPES = ["BAU", "Delivery", "Portfolio", "Standalone"]
UNCATEGORISED = "Uncategorised"

TYPE_COLOURS = ["#0C66E4", "#22A06B", "#E2B203", "#8F7EE7"]   # BAU, Delivery, Portfolio, Standalone
TYPE_LOZENGE = {"BAU": "Blue", "Delivery": "Green", "Portfolio": "Yellow", "Standalone": "Purple"}
DIVISION_COLOURS = ["#1D7F8C", "#E56910", "#5E4DB2", "#4BCE97",
                    "#AE2E24", "#2898BD", "#946F00", "#943D73"]
UNCAT_GREY = "#8590A2"
TEXT, MUTED, GRID = "#172B4D", "#626F86", "#DCDFE4"
ACTIVE_BLUE, INACTIVE_RED = "#0C66E4", "#C9372C"
plt.rcParams.update({"font.size": 10, "text.color": TEXT, "axes.labelcolor": MUTED})

AUTH = (EMAIL, TOKEN)
e = html.escape


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---------- Atlassian API ----------
def api(method, path, **kw):
    r = requests.request(method, f"{SITE}{path}", auth=AUTH, timeout=60, **kw)
    if not r.ok:
        print(f"API {method} {path.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r


def get_projects():
    projects, start = [], 0
    while True:
        data = api("GET", "/rest/api/3/project/search",
                   params={"startAt": start, "maxResults": 50, "expand": "lead"}).json()
        projects += data["values"]
        if data.get("isLast", True) or not data["values"]:
            return projects
        start += len(data["values"])


def get_page(pid):
    return api("GET", f"/wiki/api/v2/pages/{pid}").json()


def child_pages(parent_id):
    found, path = {}, f"/wiki/api/v2/pages/{parent_id}/children?limit=250"
    while path:
        data = api("GET", path).json()
        for c in data.get("results", []):
            found[c["title"]] = c["id"]
        path = data.get("_links", {}).get("next")
    return found


def create_page(space_id, parent_id, title):
    payload = {"spaceId": space_id, "status": "current", "title": title, "parentId": parent_id,
               "body": {"representation": "storage", "value": "<p>Building report...</p>"}}
    return api("POST", "/wiki/api/v2/pages", json=payload).json()["id"]


def update_page(pid, body):
    page = get_page(pid)
    payload = {"id": pid, "status": "current", "title": page["title"],
               "body": {"representation": "storage", "value": body},
               "version": {"number": page["version"]["number"] + 1,
                           "message": "Automated weekly refresh"}}
    api("PUT", f"/wiki/api/v2/pages/{pid}", json=payload)


def upload_attachment(pid, path):
    """Create or replace an attachment on a page."""
    with open(path, "rb") as f:
        api("PUT", f"/wiki/rest/api/content/{pid}/child/attachment",
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (os.path.basename(path), f, "image/png")},
            data={"minorEdit": "true"})


# ---------- Data ----------
def classify(projects):
    counts = defaultdict(lambda: defaultdict(int))  # counts[division][type]
    spaces, attention = [], []
    for p in projects:
        if not INCLUDE_TEAM_MANAGED and p.get("style") != "classic":
            continue
        cat = (p.get("projectCategory") or {}).get("name", "")
        if cat == "Template":
            continue
        lead = p.get("lead") or {}
        space = {"key": p["key"], "name": p["name"], "category": cat or "No category",
                 "lead": lead.get("displayName", ""), "lead_id": lead.get("accountId", ""),
                 "lead_active": lead.get("active", False),
                 "division": UNCATEGORISED, "type": None}
        if " | " in cat:
            div, typ = (s.strip() for s in cat.split(" | ", 1))
            if div in DIVISIONS and typ in TYPES:
                counts[div][typ] += 1
                space["division"], space["type"] = div, typ
        if space["type"] is None:
            attention.append(space)
        spaces.append(space)
    return counts, spaces, attention


def lead_register(spaces):
    leads = {}
    for s in spaces:
        rec = leads.setdefault(s["lead_id"] or "__none__", {
            "name": s["lead"] or "No lead",
            "active": s["lead_active"] if s["lead_id"] else None,
            "keys": [], "divisions": set()})
        rec["keys"].append(s["key"])
        rec["divisions"].add(s["division"])
    return sorted(leads.values(), key=lambda x: (-len(x["keys"]), x["name"].lower()))


# ---------- Confluence storage-format helpers ----------
def macro(name, params=None, body=None):
    p = "".join(f'<ac:parameter ac:name="{k}">{e(str(v))}</ac:parameter>'
                for k, v in (params or {}).items())
    b = f"<ac:rich-text-body>{body}</ac:rich-text-body>" if body is not None else ""
    return f'<ac:structured-macro ac:name="{name}">{p}{b}</ac:structured-macro>'


def status(text, colour):
    return macro("status", {"colour": colour, "title": text})


def lead_status(active):
    if active is None:
        return status("No lead", "Grey")
    return status("Active", "Green") if active else status("Inactive", "Red")


def section(kind, *cells):
    inner = "".join(f"<ac:layout-cell>{c}</ac:layout-cell>" for c in cells)
    return f'<ac:layout-section ac:type="{kind}">{inner}</ac:layout-section>'


def kpi(label, value, sub, bg):
    return macro("panel", {"bgColor": bg, "borderStyle": "none"},
                 f"<p><strong>{e(label)}</strong></p><h1>{value}</h1><p>{e(sub)}</p>")


def table(headers, rows, numeric_cols=()):
    head = "<tr>" + "".join(f"<th><p>{e(h)}</p></th>" for h in headers) + "</tr>"
    body = ""
    for r in rows:
        cells = "".join(
            (f'<td style="text-align: right;"><p>{v}</p></td>' if i in numeric_cols
             else f"<td><p>{v}</p></td>")
            for i, v in enumerate(r))
        body += f"<tr>{cells}</tr>"
    return f'<table data-layout="full-width"><tbody>{head}{body}</tbody></table>'


def img(filename, width):
    return (f'<ac:image ac:align="center" ac:layout="center" ac:width="{width}">'
            f'<ri:attachment ri:filename="{filename}"/></ac:image>')


def page_link(title, text):
    return (f'<ac:link><ri:page ri:content-title="{e(title)}"/>'
            f"<ac:plain-text-link-body><![CDATA[{text}]]></ac:plain-text-link-body></ac:link>")


def jira_link(key):
    return f'<a href="{SITE}/browse/{e(key)}">{e(key)}</a>'


# ---------- Charts (PNG) ----------
def _style(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_axisbelow(True)


def _save(fig, name, tight=True):
    if tight:
        fig.savefig(name, dpi=200, bbox_inches="tight", facecolor="white")
    else:
        fig.savefig(name, dpi=200, facecolor="white")
    plt.close(fig)
    return name


def chart_divisions(counts, uncategorised, filename):
    divs = sorted(DIVISIONS, key=lambda d: sum(counts[d].values()))   # largest at top
    rows = ([UNCATEGORISED] if uncategorised else []) + divs          # uncategorised at bottom
    ypos = list(range(len(rows)))
    fig, ax = plt.subplots(figsize=(10, 0.5 * len(rows) + 0.9))
    left = [0] * len(rows)
    for t, colour in zip(TYPES, TYPE_COLOURS):
        vals = [0 if r == UNCATEGORISED else counts[r][t] for r in rows]
        ax.barh(ypos, vals, left=left, color=colour, label=t, height=0.64,
                edgecolor="white", linewidth=1.2)
        label_colour = TEXT if t == "Portfolio" else "white"
        for i, (v, l) in enumerate(zip(vals, left)):
            if v >= 2:
                ax.text(l + v / 2, i, str(v), ha="center", va="center",
                        color=label_colour, fontsize=9, fontweight="bold")
        left = [a + b for a, b in zip(left, vals)]
    if uncategorised:
        ax.barh(0, uncategorised, color=UNCAT_GREY, label="Uncategorised", height=0.64,
                edgecolor="white", linewidth=1.2)
        left[0] = uncategorised
    peak = max(left + [1])
    for i, tot in enumerate(left):
        ax.text(tot + peak * 0.012, i, str(tot), va="center", fontsize=10, fontweight="bold")
    _style(ax)
    ax.set_yticks(ypos, rows)
    ax.set_xlim(0, peak * 1.08)
    ax.set_xlabel("Spaces")
    ax.legend(ncol=5, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, filename)


def chart_donut(labels, values, colours, centre_label, filename):
    """Fixed canvas so every donut renders at the same size on the page."""
    items = [(l, v, c) for l, v, c in zip(labels, values, colours) if v > 0]
    total = sum(v for _, v, _ in items)
    fig = plt.figure(figsize=(6, 7.4))
    ax = fig.add_axes([0.14, 0.40, 0.72, 0.58])
    if total:
        wedges, _ = ax.pie([v for _, v, _ in items], colors=[c for *_, c in items],
                           startangle=90, counterclock=False,
                           wedgeprops=dict(width=0.36, edgecolor="white", linewidth=2))
        fig.legend(wedges, [f"{l}   {v}  ({v / total:.0%})" for l, v, _ in items],
                   loc="upper center", bbox_to_anchor=(0.5, 0.37), frameon=False,
                   ncol=1, fontsize=11, handlelength=1)
    ax.text(0, 0.08, str(total), ha="center", va="center", fontsize=30, fontweight="bold")
    ax.text(0, -0.2, centre_label, ha="center", va="center", fontsize=11, color=MUTED)
    ax.set_xlim(-1.05, 1.05)
    ax.set_ylim(-1.05, 1.05)
    ax.set_aspect("equal")
    ax.axis("off")
    return _save(fig, filename, tight=False)


def chart_leads(top, filename):
    top = list(reversed(top))
    vals = [len(l["keys"]) for l in top]
    ypos = list(range(len(top)))
    fig, ax = plt.subplots(figsize=(10, max(2.4, 0.36 * len(top) + 1)))
    ax.barh(ypos, vals, height=0.62,
            color=[ACTIVE_BLUE if l["active"] else INACTIVE_RED for l in top])
    for i, v in enumerate(vals):
        ax.text(v + max(vals + [1]) * 0.01, i, str(v), va="center", fontsize=9, fontweight="bold")
    _style(ax)
    ax.set_yticks(ypos, [l["name"] for l in top])
    ax.set_xlim(0, max(vals + [1]) * 1.1)
    ax.set_xlabel("Spaces led")
    ax.legend(handles=[Patch(color=ACTIVE_BLUE, label="Active"),
                       Patch(color=INACTIVE_RED, label="Inactive")],
              ncol=2, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, filename)


# ---------- Page sections shared by parent and child ----------
def banner(now, extra=""):
    return macro("info", None,
                 f"<p>Live view of company-managed Jira spaces. Refreshed automatically every Monday - "
                 f"last refreshed <strong>{now}</strong> (Sydney). {extra}"
                 f"Do not edit this page; changes are overwritten on the next refresh.</p>")


def lead_table(leads, show_divisions=True):
    headers = ["Space lead", "Status", "Spaces"] + (["Divisions"] if show_divisions else []) + ["Space keys"]
    rows = []
    for l in leads:
        row = [e(l["name"]), lead_status(l["active"]), len(l["keys"])]
        if show_divisions:
            row.append(e(", ".join(sorted(l["divisions"]))))
        row.append(", ".join(jira_link(k) for k in sorted(l["keys"])))
        rows.append(row)
    return table(headers, rows, numeric_cols=(2,))


# ---------- Parent page ----------
def build_parent(counts, spaces, attention, now):
    total = len(spaces)
    categorised = total - len(attention)
    pct = round(categorised / total * 100) if total else 0
    leads = lead_register(spaces)
    to_review = [l for l in leads if l["active"] is not True]
    div_totals = [sum(counts[d].values()) for d in DIVISIONS]
    type_totals = [sum(counts[d][t] for d in DIVISIONS) for t in TYPES]
    top = [l for l in leads if l["active"] is not None][:TOP_LEADS]

    charts = [
        chart_divisions(counts, len(attention), "jsr_divisions.png"),
        chart_donut(TYPES, type_totals, TYPE_COLOURS, "categorised spaces", "jsr_types.png"),
        chart_donut(DIVISIONS, div_totals, DIVISION_COLOURS, "categorised spaces", "jsr_share.png"),
        chart_leads(top, "jsr_leads.png"),
    ]

    kpis = section(
        "three_equal",
        kpi("Spaces in scope", total, "Company-managed, excluding templates", "#E9F2FF"),
        kpi("Categorised", f"{pct}%", f"{categorised} of {total} on Division | Type", "#DCFFF1"),
        kpi("Leads to review", len(to_review), "Inactive or missing space lead", "#FFF7D6"),
    )

    # Division summary with links to child pages
    rows = []
    for d, tot in zip(DIVISIONS, div_totals):
        rows.append([page_link(CHILD_TITLE.format(division=d), d)]
                    + [counts[d][t] for t in TYPES] + [f"<strong>{tot}</strong>"])
    rows.append(["<strong>Total categorised</strong>"] + [f"<strong>{v}</strong>" for v in type_totals]
                + [f"<strong>{sum(type_totals)}</strong>"])
    div_table = table(["Division"] + TYPES + ["Total"], rows, numeric_cols=range(1, 6))

    # Leads: inactive table visible, full register in an expand
    inactive = [l for l in to_review]
    inactive_html = (lead_table(inactive) if inactive
                     else macro("tip", None, "<p>Every space has an active lead.</p>"))
    full_register = macro("expand", {"title": f"Show all {len(leads)} space leads"}, lead_table(leads))

    # Needs attention
    if attention:
        att_rows = [[jira_link(s["key"]), e(s["name"]), e(s["category"]), e(s["lead"] or "No lead")]
                    for s in sorted(attention, key=lambda s: s["key"])]
        att = (f"<p>{len(attention)} company-managed spaces are on a legacy category or no category. "
               f"Assign each one a <code>Division | Type</code> category to include it in the division charts.</p>"
               + table(["Key", "Space", "Current category", "Space lead"], att_rows))
    else:
        att = macro("tip", None, "<p>All company-managed spaces are categorised.</p>")

    body = (
        "<ac:layout>"
        + section("single", banner(now))
        + kpis
        + section("single", "<h2>Spaces by division</h2>" + img(charts[0], 960)
                  + f"<p><em>Grey shows the {len(attention)} spaces not yet on a Division | Type "
                    f"category - see Needs attention below.</em></p>")
        + section("two_equal",
                  "<h2>Space types</h2>" + img(charts[1], 420),
                  "<h2>Division share</h2>" + img(charts[2], 420))
        + section("single", "<h2>Division summary</h2>"
                  "<p>Select a division to open its page.</p>" + div_table)
        + section("single", f"<h2>Space leads</h2><p>Top {len(top)} leads by number of spaces.</p>"
                  + img(charts[3], 960)
                  + f"<h3>Leads to review ({len(inactive)})</h3>"
                    f"<p>Inactive or missing leads. Nominate a replacement for each space.</p>"
                  + inactive_html + full_register)
        + section("single", "<h2>Needs attention</h2>" + att)
        + "</ac:layout>"
    )
    return body, charts


# ---------- Division page ----------
def build_division(div, counts, spaces, parent_title, now):
    s_div = [s for s in spaces if s["division"] == div]
    leads = lead_register(s_div)
    to_review = [l for l in leads if l["active"] is not True]
    type_counts = [counts[div][t] for t in TYPES]
    top = [l for l in leads if l["active"] is not None][:TOP_LEADS]
    sl = slug(div)

    charts = [chart_donut(TYPES, type_counts, TYPE_COLOURS, "spaces", f"jsr_{sl}_types.png")]
    if top:
        charts.append(chart_leads(top, f"jsr_{sl}_leads.png"))

    kpis = section(
        "three_equal",
        kpi("Spaces", len(s_div), f"{div} company-managed spaces", "#E9F2FF"),
        kpi("Space leads", len(leads), "People leading these spaces", "#DCFFF1"),
        kpi("Leads to review", len(to_review), "Inactive or missing space lead", "#FFF7D6"),
    )

    type_table = table(["Space type", "Spaces"],
                       [[status(t, TYPE_LOZENGE[t]), c] for t, c in zip(TYPES, type_counts)]
                       + [["<strong>Total</strong>", f"<strong>{len(s_div)}</strong>"]],
                       numeric_cols=(1,))

    order = {t: i for i, t in enumerate(TYPES)}
    space_rows = [[jira_link(s["key"]), e(s["name"]), status(s["type"], TYPE_LOZENGE[s["type"]]),
                   e(s["lead"] or "No lead"), lead_status(s["lead_active"] if s["lead_id"] else None)]
                  for s in sorted(s_div, key=lambda s: (order[s["type"]], s["name"].lower()))]
    spaces_html = (table(["Key", "Space", "Type", "Space lead", "Lead status"], space_rows)
                   if space_rows else macro("note", None, "<p>No spaces are categorised to this division yet.</p>"))

    leads_html = ""
    if top:
        leads_html = (img(charts[1], 960)
                      + (lead_table(to_review, show_divisions=False) if to_review else "")
                      + macro("expand", {"title": f"Show all {len(leads)} space leads"},
                              lead_table(leads, show_divisions=False)))

    body = (
        "<ac:layout>"
        + section("single", banner(now, f"Part of {page_link(parent_title, parent_title)}. "))
        + kpis
        + section("two_equal", "<h2>Space types</h2>" + img(charts[0], 420),
                  "<h2>Breakdown</h2>" + type_table)
        + section("single", "<h2>Spaces</h2>" + spaces_html)
        + (section("single", "<h2>Space leads</h2>" + leads_html) if leads_html else "")
        + "</ac:layout>"
    )
    return body, charts


# ---------- Main ----------
if __name__ == "__main__":
    if not (EMAIL and TOKEN and (PAGE_ID or DRY_RUN)):
        raise SystemExit("Missing ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN or CONFLUENCE_PARENT_PAGE_ID")
    now = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%A %d %B %Y, %I:%M %p")

    projects = get_projects()
    print(f"Jira returned {len(projects)} projects")
    if not projects:
        raise SystemExit("Jira returned 0 projects - check the email and API token secrets")
    counts, spaces, attention = classify(projects)
    print(f"{len(spaces)} in scope, {len(attention)} need attention")

    if DRY_RUN:
        for div in DIVISIONS:
            body, _ = build_division(div, counts, spaces, "Jira space portfolio", now)
            open(f"preview_{slug(div)}.html", "w", encoding="utf-8").write(body)
        body, _ = build_parent(counts, spaces, attention, now)
        open("preview_parent.html", "w", encoding="utf-8").write(body)
        raise SystemExit("DRY RUN: previews and chart PNGs written")

    parent = get_page(PAGE_ID)
    children = child_pages(PAGE_ID)
    for div in DIVISIONS:
        title = CHILD_TITLE.format(division=div)
        cid = children.get(title)
        if not cid:
            cid = create_page(parent["spaceId"], PAGE_ID, title)
            print(f"Created page: {title}")
        body, files = build_division(div, counts, spaces, parent["title"], now)
        for f in files:
            upload_attachment(cid, f)
        update_page(cid, body)
        print(f"Updated: {title}")

    body, files = build_parent(counts, spaces, attention, now)
    for f in files:
        upload_attachment(PAGE_ID, f)
    update_page(PAGE_ID, body)
    print(f"Updated parent page: {parent['title']}")
