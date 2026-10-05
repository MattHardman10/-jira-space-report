"""
Jira Space Report - parent overview page (REST Super)

Pulls every Jira space, splits "Division | Space Type" categories and
rewrites a Confluence page with KPIs, charts, a division summary,
a space lead register and a "needs attention" list.

Env vars (GitHub Actions secrets):
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, CONFLUENCE_PARENT_PAGE_ID
Optional:
  DRY_RUN=1  -> writes preview.html locally instead of updating Confluence
"""
import os
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
INCLUDE_TEAM_MANAGED = False   # team-managed spaces are out of scope
TOP_LEADS = 15                 # leads shown in the bar chart

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

TYPE_COLOURS = ["#0C66E4", "#22A06B", "#E2B203", "#8F7EE7"]          # BAU, Delivery, Portfolio, Standalone
DIVISION_COLOURS = ["#1D7F8C", "#E56910", "#5E4DB2", "#4BCE97",
                    "#AE2E24", "#2898BD", "#946F00", "#943D73"]
TEXT, MUTED, GRID = "#172B4D", "#626F86", "#DCDFE4"
ACTIVE_BLUE, INACTIVE_RED = "#0C66E4", "#C9372C"
plt.rcParams.update({"font.size": 10, "text.color": TEXT, "axes.labelcolor": MUTED})

AUTH = (EMAIL, TOKEN)
e = html.escape


# ---------- Data ----------
def get_projects():
    projects, start = [], 0
    while True:
        r = requests.get(
            f"{SITE}/rest/api/3/project/search",
            auth=AUTH,
            params={"startAt": start, "maxResults": 50, "expand": "lead"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        projects += data["values"]
        if data.get("isLast", True) or not data["values"]:
            return projects
        start += len(data["values"])


def classify(projects):
    """Returns counts[division][type], all in-scope spaces, and spaces needing attention."""
    counts = defaultdict(lambda: defaultdict(int))
    spaces, attention = [], []
    for p in projects:
        if not INCLUDE_TEAM_MANAGED and p.get("style") != "classic":
            continue
        cat = (p.get("projectCategory") or {}).get("name", "")
        if cat == "Template":
            continue
        lead = p.get("lead") or {}
        space = {
            "key": p["key"],
            "name": p["name"],
            "category": cat or "No category",
            "lead": lead.get("displayName", ""),
            "lead_id": lead.get("accountId", ""),
            "lead_active": lead.get("active", False),
            "division": UNCATEGORISED,
            "type": None,
        }
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
        k = s["lead_id"] or "__none__"
        rec = leads.setdefault(k, {
            "name": s["lead"] or "No lead",
            "active": s["lead_active"] if s["lead_id"] else None,
            "keys": [], "divisions": set(),
        })
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
        cells = ""
        for i, v in enumerate(r):
            style = ' style="text-align: right;"' if i in numeric_cols else ""
            cells += f"<td{style}><p>{v}</p></td>"
        body += f"<tr>{cells}</tr>"
    return f'<table data-layout="full-width"><tbody>{head}{body}</tbody></table>'


def img(filename, width):
    return (f'<ac:image ac:align="center" ac:layout="center" ac:width="{width}">'
            f'<ri:attachment ri:filename="{filename}"/></ac:image>')


# ---------- Charts (PNG) ----------
def _style(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_axisbelow(True)


def _save(fig, name):
    fig.savefig(name, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return name


def chart_divisions(counts):
    divs = sorted(DIVISIONS, key=lambda d: sum(counts[d].values()))  # largest at top
    fig, ax = plt.subplots(figsize=(10, 4.6))
    ypos = list(range(len(divs)))
    left = [0] * len(divs)
    for t, colour in zip(TYPES, TYPE_COLOURS):
        vals = [counts[d][t] for d in divs]
        ax.barh(ypos, vals, left=left, color=colour, label=t, height=0.64,
                edgecolor="white", linewidth=1.2)
        label_colour = TEXT if t == "Portfolio" else "white"
        for i, (v, l) in enumerate(zip(vals, left)):
            if v >= 2:
                ax.text(l + v / 2, i, str(v), ha="center", va="center",
                        color=label_colour, fontsize=9, fontweight="bold")
        left = [a + b for a, b in zip(left, vals)]
    for i, tot in enumerate(left):
        ax.text(tot + max(left) * 0.012 + 0.2, i, str(tot), va="center",
                fontsize=10, fontweight="bold")
    _style(ax)
    ax.set_yticks(ypos, divs)
    ax.set_xlim(0, max(left + [1]) * 1.08)
    ax.set_xlabel("Spaces")
    ax.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, "jsr_divisions.png")


def chart_donut(labels, values, colours, centre_label, filename):
    items = [(l, v, c) for l, v, c in zip(labels, values, colours) if v > 0]
    total = sum(v for _, v, _ in items)
    fig, ax = plt.subplots(figsize=(5.4, 5.6))
    if total:
        wedges, _ = ax.pie([v for _, v, _ in items], colors=[c for *_, c in items],
                           startangle=90, counterclock=False,
                           wedgeprops=dict(width=0.36, edgecolor="white", linewidth=2))
        ax.legend(wedges, [f"{l}   {v}  ({v / total:.0%})" for l, v, _ in items],
                  loc="upper center", bbox_to_anchor=(0.5, 0.02), frameon=False,
                  ncol=2, fontsize=9, handlelength=1, columnspacing=1.5)
    ax.text(0, 0.08, str(total), ha="center", va="center", fontsize=28, fontweight="bold")
    ax.text(0, -0.2, centre_label, ha="center", va="center", fontsize=10, color=MUTED)
    ax.set_aspect("equal")
    return _save(fig, filename)


def chart_leads(top):
    top = list(reversed(top))
    vals = [len(l["keys"]) for l in top]
    fig, ax = plt.subplots(figsize=(10, max(3, 0.36 * len(top) + 1)))
    ypos = list(range(len(top)))
    ax.barh(ypos, vals, height=0.62,
            color=[ACTIVE_BLUE if l["active"] else INACTIVE_RED for l in top])
    ax.set_yticks(ypos, [l["name"] for l in top])
    for i, v in enumerate(vals):
        ax.text(v + 0.06, i, str(v), va="center", fontsize=9, fontweight="bold")
    _style(ax)
    ax.set_xlim(0, max(vals + [1]) * 1.1)
    ax.set_xlabel("Spaces led")
    ax.legend(handles=[Patch(color=ACTIVE_BLUE, label="Active"),
                       Patch(color=INACTIVE_RED, label="Inactive")],
              ncol=2, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
    return _save(fig, "jsr_leads.png")


def upload_attachment(path):
    """Create or replace an attachment on the report page."""
    url = f"{SITE}/wiki/rest/api/content/{PAGE_ID}/child/attachment"
    with open(path, "rb") as f:
        r = requests.put(url, auth=AUTH, headers={"X-Atlassian-Token": "no-check"},
                         files={"file": (os.path.basename(path), f, "image/png")},
                         data={"minorEdit": "true"}, timeout=60)
    if not r.ok:
        print("Attachment upload failed:", r.status_code, r.text[:300])
    r.raise_for_status()


# ---------- Page ----------
def build_page(counts, spaces, attention):
    now = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%A %d %B %Y, %I:%M %p")
    total = len(spaces)
    categorised = total - len(attention)
    pct = round(categorised / total * 100) if total else 0
    leads = lead_register(spaces)
    leads_to_review = [l for l in leads if l["active"] is not True]
    div_totals = [sum(counts[d].values()) for d in DIVISIONS]
    type_totals = [sum(counts[d][t] for d in DIVISIONS) for t in TYPES]

    # Header
    intro = macro("info", None,
                  f"<p>Live view of company-managed Jira spaces by division and space type. "
                  f"Refreshed automatically every Monday - last refreshed <strong>{now}</strong> (Sydney). "
                  f"Do not edit this page; changes are overwritten on the next refresh.</p>")

    # KPIs
    kpis = section(
        "three_equal",
        kpi("Spaces in scope", total, "Company-managed, excluding templates", "#E9F2FF"),
        kpi("Categorised", f"{pct}%", f"{categorised} of {total} on Division | Type", "#DCFFF1"),
        kpi("Leads to review", len(leads_to_review), "Inactive or missing space lead", "#FFF7D6"),
    )

    # Charts (PNG files, uploaded as page attachments)
    top = [l for l in leads if l["active"] is not None][:TOP_LEADS]
    charts = {
        "divisions": chart_divisions(counts),
        "types": chart_donut(TYPES, type_totals, TYPE_COLOURS, "spaces", "jsr_types.png"),
        "share": chart_donut(DIVISIONS, div_totals, DIVISION_COLOURS, "spaces", "jsr_share.png"),
        "leads": chart_leads(top),
    }
    division_chart = img(charts["divisions"], 960)
    type_pie = img(charts["types"], 440)
    div_pie = img(charts["share"], 440)

    # Division summary table
    rows = []
    for d, tot in zip(DIVISIONS, div_totals):
        rows.append([e(d)] + [counts[d][t] for t in TYPES] + [f"<strong>{tot}</strong>"])
    rows.append(["<strong>Total</strong>"] + [f"<strong>{v}</strong>" for v in type_totals]
                + [f"<strong>{sum(type_totals)}</strong>"])
    div_table = table(["Division"] + TYPES + ["Total"], rows, numeric_cols=range(1, 6))

    # Space leads
    lead_chart = img(charts["leads"], 960)

    def lead_status(l):
        if l["active"] is None:
            return status("No lead", "Grey")
        return status("Active", "Green") if l["active"] else status("Inactive", "Red")

    lead_rows = [[
        e(l["name"]), lead_status(l), len(l["keys"]),
        e(", ".join(sorted(l["divisions"]))), e(", ".join(sorted(l["keys"]))),
    ] for l in leads]
    lead_table = table(["Space lead", "Status", "Spaces", "Divisions", "Space keys"],
                       lead_rows, numeric_cols=(2,))

    # Needs attention
    if attention:
        att_rows = [[e(s["key"]), e(s["name"]), e(s["category"]), e(s["lead"] or "No lead")]
                    for s in sorted(attention, key=lambda s: s["key"])]
        att = (f"<p>{len(attention)} company-managed spaces are on a legacy category or no category. "
               f"Assign each one a <code>Division | Type</code> category to include it in the charts.</p>"
               + table(["Key", "Space", "Current category", "Space lead"], att_rows))
    else:
        att = macro("tip", None, "<p>All company-managed spaces are categorised.</p>")

    body = (
        "<ac:layout>"
        + section("single", intro)
        + kpis
        + section("single", "<h2>Spaces by division</h2>" + division_chart)
        + section("two_equal", "<h2>Space types</h2>" + type_pie,
                  "<h2>Division share</h2>" + div_pie)
        + section("single", "<h2>Division summary</h2>" + div_table)
        + section("single", f"<h2>Space leads</h2>"
                  f"<p>Top {len(top)} leads by number of spaces, then every lead with their account status. "
                  f"Inactive leads should be replaced.</p>" + lead_chart + lead_table)
        + section("single", "<h2>Needs attention</h2>" + att)
        + "</ac:layout>"
    )
    return body, list(charts.values())


def update_page(body):
    url = f"{SITE}/wiki/api/v2/pages/{PAGE_ID}"
    r = requests.get(url, auth=AUTH, timeout=30)
    if not r.ok:
        me = requests.get(f"{SITE}/wiki/rest/api/user/current", auth=AUTH, timeout=30)
        who = me.json() if me.ok else {}
        print("DIAG status:", r.status_code)
        print("DIAG page id length:", len(PAGE_ID), "| digits only:", PAGE_ID.isdigit())
        print("DIAG Confluence sees you as:", who.get("type"), "|", who.get("displayName"))
        print("DIAG response:", r.text[:300])
        raise SystemExit("Could not read the Confluence page - see DIAG lines above")
    page = r.json()
    payload = {
        "id": PAGE_ID,
        "status": "current",
        "title": page["title"],
        "body": {"representation": "storage", "value": body},
        "version": {"number": page["version"]["number"] + 1,
                    "message": "Automated weekly refresh"},
    }
    r = requests.put(url, auth=AUTH, json=payload, timeout=30)
    if not r.ok:
        print(r.text)
    r.raise_for_status()


if __name__ == "__main__":
    if not (EMAIL and TOKEN and (PAGE_ID or DRY_RUN)):
        raise SystemExit("Missing ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN or CONFLUENCE_PARENT_PAGE_ID")
    projects = get_projects()
    print(f"Jira returned {len(projects)} projects")
    counts, spaces, attention = classify(projects)
    body, chart_files = build_page(counts, spaces, attention)
    if DRY_RUN:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(body)
        print(f"DRY RUN: {len(projects)} projects read, {len(spaces)} in scope, preview.html and chart PNGs written")
    else:
        for path in chart_files:
            upload_attachment(path)
        update_page(body)
        print(f"Updated page {PAGE_ID}: {len(spaces)} spaces in scope, {len(attention)} need attention")
