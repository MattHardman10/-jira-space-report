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

# ---------- Settings ----------
SITE = "https://restsuper.atlassian.net"
EMAIL = os.environ.get("ATLASSIAN_EMAIL", "")
TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "")
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

TYPE_COLOURS = "#0C66E4,#22A06B,#E2B203,#8F7EE7"
DIVISION_COLOURS = "#0C66E4,#22A06B,#E2B203,#8F7EE7,#F87168,#2898BD,#B38600,#6E5DC6"

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


def chart(title, ctype, categories, series, colours, width=900, height=420,
          stacked=False, orientation=None, x_label="", y_label=""):
    """series = [(series_name, [values...]), ...] aligned to categories."""
    params = {"type": ctype, "title": title, "width": width, "height": height,
              "legend": "true", "dataOrientation": "horizontal", "colors": colours,
              "showShapes": "false"}
    if stacked:
        params["stacked"] = "true"
    if orientation:
        params["orientation"] = orientation
    if x_label:
        params["xLabel"] = x_label
    if y_label:
        params["yLabel"] = y_label
    head = "<tr><th><p></p></th>" + "".join(f"<th><p>{e(c)}</p></th>" for c in categories) + "</tr>"
    body = "".join(
        f"<tr><th><p>{e(name)}</p></th>" + "".join(f"<td><p>{v}</p></td>" for v in vals) + "</tr>"
        for name, vals in series
    )
    return macro("chart", params, f"<table><tbody>{head}{body}</tbody></table>")


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

    # Division chart (stacked by type)
    division_chart = chart(
        "Spaces by division and space type", "bar", DIVISIONS,
        [(t, [counts[d][t] for d in DIVISIONS]) for t in TYPES],
        TYPE_COLOURS, width=1000, height=460, stacked=True,
        orientation="horizontal", y_label="Spaces",
    )

    # Type pie + division pie side by side
    type_pie = chart("Space types across REST", "pie", TYPES,
                     [("Spaces", type_totals)], TYPE_COLOURS, width=480, height=380)
    div_pie = chart("Share of spaces by division", "pie", DIVISIONS,
                    [("Spaces", div_totals)], DIVISION_COLOURS, width=480, height=380)

    # Division summary table
    rows = []
    for d, tot in zip(DIVISIONS, div_totals):
        rows.append([e(d)] + [counts[d][t] for t in TYPES] + [f"<strong>{tot}</strong>"])
    rows.append(["<strong>Total</strong>"] + [f"<strong>{v}</strong>" for v in type_totals]
                + [f"<strong>{sum(type_totals)}</strong>"])
    div_table = table(["Division"] + TYPES + ["Total"], rows, numeric_cols=range(1, 6))

    # Space leads
    top = [l for l in leads if l["active"] is not None][:TOP_LEADS]
    lead_chart = chart(f"Spaces per lead (top {len(top)})", "bar",
                       [l["name"] for l in top], [("Spaces", [len(l["keys"]) for l in top])],
                       "#0C66E4", width=1000, height=max(300, 32 * len(top)),
                       orientation="horizontal")

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

    return (
        "<ac:layout>"
        + section("single", intro)
        + kpis
        + section("single", "<h2>Spaces by division</h2>" + division_chart)
        + section("two_equal", "<h2>Space types</h2>" + type_pie,
                  "<h2>Division share</h2>" + div_pie)
        + section("single", "<h2>Division summary</h2>" + div_table)
        + section("single", "<h2>Space leads</h2>"
                  "<p>Every space lead, how many spaces they own and their account status. "
                  "Inactive leads should be replaced.</p>" + lead_chart + lead_table)
        + section("single", "<h2>Needs attention</h2>" + att)
        + "</ac:layout>"
    )


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
    body = build_page(counts, spaces, attention)
    if DRY_RUN:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(body)
        print(f"DRY RUN: {len(projects)} projects read, {len(spaces)} in scope, preview.html written")
    else:
        update_page(body)
        print(f"Updated page {PAGE_ID}: {len(spaces)} spaces in scope, {len(attention)} need attention")
