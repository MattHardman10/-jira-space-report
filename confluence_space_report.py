"""
Confluence Space Report (REST Super)

Weekly refresh of a live Confluence page showing the health of all Confluence
spaces: headline numbers, the 5 newest spaces, top space owners, activity,
and a "needs attention" list. Charts are drawn as PNGs and
uploaded to the page as attachments.

Env vars (GitHub Actions secrets):
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, CONFLUENCE_SPACE_REPORT_PAGE_ID
Optional:
  DRY_RUN=1  -> writes preview.html + PNGs locally instead of updating Confluence
"""
import os
import html
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

# ---------- Settings ----------
SITE = "https://restsuper.atlassian.net"
EMAIL = os.environ.get("ATLASSIAN_EMAIL", "").strip()
TOKEN = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
PAGE_ID = os.environ.get("CONFLUENCE_SPACE_REPORT_PAGE_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"

NEWEST_SPACES = 5        # "newest spaces" table
TOP_OWNERS = 10          # owners chart / table
WORKERS = 6              # parallel API calls for per-space lookups
SYDNEY = ZoneInfo("Australia/Sydney")

TEXT, MUTED, GRID = "#172B4D", "#626F86", "#DCDFE4"
BLUE, RED, GREEN, AMBER, GREY = "#0C66E4", "#C9372C", "#22A06B", "#E2B203", "#8590A2"
BUCKETS = [("Last 30 days", GREEN), ("1-6 months", BLUE), ("6-12 months", AMBER),
           ("Over 12 months", RED), ("No content", GREY)]
plt.rcParams.update({"font.size": 10, "text.color": TEXT, "axes.labelcolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED})

AUTH = (EMAIL, TOKEN)
e = html.escape
NOW = datetime.now(timezone.utc)


# ---------- Atlassian API ----------
def api(method, path, retries=4, **kw):
    url = path if path.startswith("http") else f"{SITE}{path}"
    for attempt in range(retries):
        r = requests.request(method, url, auth=AUTH, timeout=60, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 2 ** attempt * 2)))
            continue
        if not r.ok:
            print(f"API {method} {url.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def get_spaces():
    """All spaces (current + archived) from the v2 API, cursor-paginated."""
    spaces = []
    for status in ("current", "archived"):
        url, params = "/wiki/api/v2/spaces", {"limit": 250, "status": status}
        while url:
            d = api("GET", url, params=params).json()
            spaces += d.get("results", [])
            nxt = d.get("_links", {}).get("next")
            url, params = (nxt if nxt else None), None
    return spaces


def cql(query, limit=1):
    return api("GET", "/wiki/rest/api/search", params={"cql": query, "limit": limit}).json()


def space_stats(key):
    """Last update and total pages for one space."""
    q = f'space="{key}" and type in (page,blogpost)'
    try:
        latest = cql(q + " order by lastmodified desc")
        res = latest.get("results", [])
        last = parse_date(res[0].get("lastModified")) if res else None
        pages = latest.get("totalSize")
    except requests.HTTPError:
        return key, {"last": None, "pages": None, "error": True}
    return key, {"last": last, "pages": pages, "error": False}


def get_users(account_ids):
    """Display name + active flag. Jira's bulk endpoint returns the 'active' flag."""
    users, ids = {}, [a for a in account_ids if a]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            vals = api("GET", "/rest/api/3/user/bulk",
                       params=[("accountId", a) for a in chunk] + [("maxResults", 100)]).json().get("values", [])
            for u in Filter(none, vals):
                users[u["accountId"]] = {"name": u.get("displayName", "Unknown"), "active": u.get("active", True)}
        except requests.HTTPError:
            pass
        for a in chunk:  # fallback to Confluence for anyone Jira didn't return
            if a not in users:
                try:
                    u = api("GET", "/wiki/rest/api/user", params={"accountId": a}).json()
                    users[a] = {"name": u.get("displayName") or u.get("publicName") or "Unknown",
                                "active": u.get("accountStatus", "active") == "active"}
                except requests.HTTPError:
                    users[a] = {"name": "Unknown user", "active": False}
    return users


# ---------- Helpers ----------
def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt(d):
    return d.astimezone(SYDNEY).strftime("%d %b %Y") if d else "—"


def bucket(last):
    if last is None:
        return "No content"
    days = (NOW - last).days
    if days <= 30:
        return "Last 30 days"
    if days <= 182:
        return "1-6 months"
    if days <= 365:
        return "6-12 months"
    return "Over 12 months"


def space_link(s):
    return f'<a href="{SITE}/wiki/spaces/{e(s["key"])}">{e(s["name"])}</a>'


def lozenge(text, colour):
    return (f'<ac:structured-macro ac:name="status"><ac:parameter ac:name="colour">{colour}</ac:parameter>'
            f'<ac:parameter ac:name="title">{e(text)}</ac:parameter></ac:structured-macro>')


def person(uid, users):
    if not uid:
        return lozenge("No owner", "Red")
    u = users.get(uid, {"name": "Unknown user", "active": False})
    return e(u["name"]) + ("" if u["active"] else " " + lozenge("Inactive", "Red"))


def table(headers, rows):
    h = "".join(f"<th><p><strong>{x}</strong></p></th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td><p>{c}</p></td>" for c in r) + "</tr>" for r in rows)
    return f'<table data-layout="full-width"><tbody><tr>{h}</tr>{b}</tbody></table>'


def expand(title, body):
    return (f'<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">{e(title)}</ac:parameter>'
            f"<ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>")


def panel(kind, body):
    return f'<ac:structured-macro ac:name="{kind}"><ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>'


def image(fname, width=900):
    return f'<p><ac:image ac:width="{width}"><ri:attachment ri:filename="{fname}" /></ac:image></p>'


# ---------- Charts ----------
def style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)


def chart_created(team):
    months = []
    d = NOW.astimezone(SYDNEY).replace(day=1)
    for _ in range(12):
        months.append(d.strftime("%Y-%m"))
        d = (d - timedelta(days=1)).replace(day=1)
    months.reverse()
    counts = Counter(s["created"].astimezone(SYDNEY).strftime("%Y-%m") for s in team if s["created"])
    vals = [counts.get(m, 0) for m in months]
    labels = [datetime.strptime(m, "%Y-%m").strftime("%b %y") for m in months]
    fig, ax = plt.subplots(figsize=(10, 3.4), dpi=150)
    bars = ax.bar(labels, vals, color=BLUE, width=0.6)
    for b, v in zip(bars, vals):
        if v:
            ax.text(b.get_x() + b.get_width() / 2, v, str(v), ha="center", va="bottom", fontsize=9, color=TEXT)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_title("New spaces created per month (last 12 months)", loc="left", fontsize=12, fontweight="bold")
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    style(ax)
    fig.tight_layout()
    fig.savefig("csr_created.png")
    plt.close(fig)
    return "csr_created.png"


def chart_activity(bucket_counts, total):
    labels = [b for b, _ in BUCKETS if bucket_counts.get(b)]
    vals = [bucket_counts[b] for b in labels]
    cols = [c for b, c in BUCKETS if bucket_counts.get(b)]
    fig, ax = plt.subplots(figsize=(6.5, 3.6), dpi=150)
    ax.pie(vals, colors=cols, startangle=90, counterclock=False,
           wedgeprops={"width": 0.38, "edgecolor": "white", "linewidth": 2})
    ax.text(0, 0.08, str(total), ha="center", va="center", fontsize=20, fontweight="bold")
    ax.text(0, -0.2, "current spaces", ha="center", va="center", fontsize=9, color=MUTED)
    ax.legend([f"{l}  {v} ({v / total:.0%})" for l, v in zip(labels, vals)], loc="center left",
              bbox_to_anchor=(1, 0.5), frameon=False)
    ax.set_title("Last updated", loc="left", fontsize=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig("csr_activity.png")
    plt.close(fig)
    return "csr_activity.png"


def chart_owners(top, users):
    names = [users.get(uid, {}).get("name", "Unknown") for uid, _ in top][::-1]
    vals = [n for _, n in top][::-1]
    cols = [BLUE if users.get(uid, {}).get("active", False) else RED for uid, _ in top][::-1]
    fig, ax = plt.subplots(figsize=(10, 0.42 * len(top) + 1), dpi=150)
    bars = ax.barh(names, vals, color=cols, height=0.6)
    for b, v in zip(bars, vals):
        ax.text(v + 0.1, b.get_y() + b.get_height() / 2, str(v), va="center", fontsize=9)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_title(f"Top {len(top)} space owners (red = inactive account)", loc="left", fontsize=12, fontweight="bold")
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    style(ax)
    fig.tight_layout()
    fig.savefig("csr_owners.png")
    plt.close(fig)
    return "csr_owners.png"


# ---------- Build ----------
def collect():
    raw = get_spaces()
    print(f"Spaces returned: {len(raw)}")
    spaces = []
    for s in raw:
        owner = s.get("spaceOwnerId") or s.get("authorId")
        spaces.append({"id": s.get("id"), "key": s.get("key", ""), "name": s.get("name", ""),
                       "type": s.get("type", ""), "status": s.get("status", ""),
                       "created": parse_date(s.get("createdAt")), "owner": owner,
                       "creator": s.get("authorId"), "owner_is_set": bool(s.get("spaceOwnerId"))})
    team = [s for s in spaces if s["type"] != "personal" and s["status"] == "current"]
    print(f"Current team spaces: {len(team)} - fetching activity...")
    with ThreadPoolExecutor(WORKERS) as pool:
        stats = dict(pool.map(space_stats, [s["key"] for s in team]))
    for s in team:
        s.update(stats.get(s["key"], {"last": None, "pages": None, "error": True}))
    users = get_users(sorted({s["owner"] for s in team if s["owner"]} | {s["creator"] for s in team if s["creator"]}))
    return spaces, team, users


def build_page(spaces, team, users):
    archived = [s for s in spaces if s["type"] != "personal" and s["status"] == "archived"]
    personal = [s for s in spaces if s["type"] == "personal"]
    for s in team:
        s["bucket"] = bucket(s["last"])
    bc = Counter(s["bucket"] for s in team)
    new90 = [s for s in team if s["created"] and (NOW - s["created"]).days <= 90]
    no_owner = [s for s in team if not s["owner"]]
    inactive_owner = [s for s in team if s["owner"] and not users.get(s["owner"], {}).get("active", False)]
    stale = sorted([s for s in team if s["bucket"] in ("Over 12 months", "No content")],
                   key=lambda s: s["last"] or datetime.min.replace(tzinfo=timezone.utc))
    updated30 = bc.get("Last 30 days", 0)

    owned = Counter(s["owner"] for s in team if s["owner"])
    top = owned.most_common(TOP_OWNERS)
    newest = sorted([s for s in team if s["created"]], key=lambda s: s["created"], reverse=True)[:NEWEST_SPACES]

    files = [chart_created(team), chart_activity(bc, len(team) or 1)]
    if top:
        files.append(chart_owners(top, users))

    stamp = NOW.astimezone(SYDNEY).strftime("%A %d %B %Y, %I:%M %p")
    kpis = [("Current spaces", len(team), BLUE), ("New (last 90 days)", len(new90), GREEN),
            ("Updated in last 30 days", updated30, GREEN), ("Not updated 12m+ or empty", len(stale), RED),
            ("Owner inactive or missing", len(inactive_owner) + len(no_owner), RED)]
            
    kpi_cells = "".join(
        f'<td style="text-align:center"><p style="text-align:center"><span style="color:{c}"><strong>'
        f'<span style="font-size:24px">{v}</span></strong></span></p>'
        f'<p style="text-align:center"><span style="color:{MUTED}">{e(l)}</span></p></td>' for l, v, c in kpis)

    p = []
    p.append(panel("info", f"<p>Live report of all Confluence spaces. Last refreshed <strong>{stamp}</strong> (Sydney). "
                           f"Refreshes automatically every Monday. This page is overwritten on each run, so please don't edit it.</p>"))
    p.append(f'<table data-layout="full-width"><tbody><tr>{kpi_cells}</tr></tbody></table>')

    n = max(len(team), 1)
    top_inactive = [(uid, k) for uid, k in top if not users.get(uid, {}).get("active", False)]
    points = [
        f"<strong>{len(team)}</strong> current spaces. <strong>{updated30}</strong> ({updated30 / n:.0%}) were updated "
        f"in the last 30 days, and <strong>{len(stale)}</strong> ({len(stale) / n:.0%}) have not been updated in over "
        f"12 months or have no content. These are archive candidates.",
        f"<strong>{len(inactive_owner) + len(no_owner)}</strong> spaces have an inactive or missing owner, "
        f"so nobody is accountable for their content.",
        f"<strong>{len(new90)}</strong> new spaces were created in the last 90 days.",
    ]
    if top_inactive:
        uid, k = top_inactive[0]
        points.append(f"The largest gap is <strong>{e(users.get(uid, {}).get('name', 'Unknown'))}</strong> "
                      f"(inactive), who still owns <strong>{k}</strong> spaces.")
    p.append("<h2>Summary</h2><ul>" + "".join(f"<li><p>{x}</p></li>" for x in points) + "</ul>")

    p.append("<h2>Newest spaces</h2>")
    p.append(f"<p>The {len(newest)} most recently created spaces.</p>")
    p.append(table(["Space", "Key", "Created", "Space owner", "Created by"],
                   [[space_link(s), e(s["key"]), fmt(s["created"]), person(s["owner"], users),
                     person(s["creator"], users)] for s in newest]))
    p.append(image(files[0]))

    p.append("<h2>Space activity</h2>")
    p.append("<p>When content in each current space was last created or updated.</p>")
    p.append(image(files[1], 620))
    p.append(table(["Last updated", "Spaces", "Share"],
                   [[b, str(bc.get(b, 0)), f"{bc.get(b, 0) / max(len(team), 1):.0%}"] for b, _ in BUCKETS]))

    p.append("<h2>Top space owners</h2>")
    p.append("<p>People who own the most current spaces. An inactive owner means nobody is accountable for those spaces.</p>")
    if top:
        p.append(image(files[2]))
        p.append(table(["Space owner", "Spaces owned", "Account"],
                       [[e(users.get(uid, {}).get("name", "Unknown")), str(n),
                         lozenge("Active", "Green") if users.get(uid, {}).get("active") else lozenge("Inactive", "Red")]
                        for uid, n in top]))


    p.append("<h2>Needs attention</h2>")
    att = sorted(no_owner + inactive_owner, key=lambda s: s["name"].lower())
    p.append(f"<p><strong>{len(att)}</strong> spaces have no owner or an inactive owner, and "
             f"<strong>{len(stale)}</strong> have not been updated in over 12 months or have no content.</p>")
    p.append(expand(f"Spaces with no owner or an inactive owner ({len(att)})",
                    table(["Space", "Key", "Space owner", "Last updated"],
                          [[space_link(s), e(s["key"]), person(s["owner"], users), fmt(s["last"])] for s in att]
                          or [["None", "", "", ""]])))
    p.append(expand(f"Stale or empty spaces - archive candidates ({len(stale)})",
                    table(["Space", "Key", "Space owner", "Last updated", "Pages"],
                          [[space_link(s), e(s["key"]), person(s["owner"], users), fmt(s["last"]),
                            str(s["pages"]) if s["pages"] is not None else "—"] for s in stale]
                          or [["None", "", "", "", ""]])))

    errs = sum(1 for s in team if s.get("error"))
    notes = ("<p><strong>Notes:</strong> Space owner is the Confluence space owner field; where it isn't set, the person "
             "who created the space is shown. Personal spaces are counted but not analysed. Last updated covers pages and blogs.")
    if errs:
        notes += f" Activity could not be read for {errs} spaces (shown as —)."
    p.append(notes + "</p>")
    return "".join(p), files


# ---------- Confluence page ----------
def upload_attachment(path):
    with open(path, "rb") as f:
        api("PUT", f"/wiki/rest/api/content/{PAGE_ID}/child/attachment",
            headers={"X-Atlassian-Token": "nocheck"},
            files={"file": (os.path.basename(path), f, "image/png")}, data={"minorEdit": "true"})


def update_page(body):
    cur = api("GET", f"/wiki/rest/api/content/{PAGE_ID}", params={"expand": "version"}).json()
    api("PUT", f"/wiki/rest/api/content/{PAGE_ID}", json={
        "id": PAGE_ID, "type": "page", "title": cur["title"],
        "version": {"number": cur["version"]["number"] + 1, "message": "Weekly refresh"},
        "body": {"storage": {"value": body, "representation": "storage"}},
    })


def set_full_width():
    """Set the page to full width (content-appearance properties). Never fails the run."""
    for key in ("content-appearance-published", "content-appearance-draft"):
        try:
            base = f"/wiki/api/v2/pages/{PAGE_ID}/properties"
            found = api("GET", base, params={"key": key}).json().get("results", [])
            if found:
                prop = found[0]
                if prop.get("value") != "full-width":
                    api("PUT", f"{base}/{prop['id']}", json={
                        "key": key, "value": "full-width",
                        "version": {"number": prop["version"]["number"] + 1}})
            else:
                api("POST", base, json={"key": key, "value": "full-width"})
        except requests.HTTPError:
            print(f"Could not set {key} - set full width manually if the page looks narrow")

def main():
  
    if not (EMAIL and TOKEN):
        raise SystemExit("Missing ATLASSIAN_EMAIL or ATLASSIAN_API_TOKEN")
    if not DRY_RUN and not PAGE_ID.isdigit():
        raise SystemExit("CONFLUENCE_SPACE_REPORT_PAGE_ID must be the number from the page URL")
    me = api("GET", "/wiki/rest/api/user/current").json()
    if me.get("type") == "anonymous" or not me.get("accountId"):
        raise SystemExit("Confluence sees you as anonymous - the API token is wrong. Use a standard "
                         "(not scoped) token from id.atlassian.com and update the GitHub secret.")
    print(f"Logged in as: {me.get('displayName')}")
    spaces, team, users = collect()
    body, files = build_page(spaces, team, users)
    if DRY_RUN:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(body.replace('<ri:attachment ri:filename="', '<img src="').replace('" /></ac:image>', '"></ac:image>'))
        print("DRY RUN: preview.html and chart PNGs written")
        return
    for path in files:
        upload_attachment(path)
    update_page(body)
    set_full_width()
    print(f"DONE. Page updated: {SITE}/wiki/pages/viewpage.action?pageId={PAGE_ID}")


if __name__ == "__main__":
    main()
