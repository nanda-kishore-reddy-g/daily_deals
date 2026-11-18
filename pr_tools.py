"""Daily deals helper: Serper (find deals + PR link) and Firecrawl (PR text).

Used by the 7pm Claude scheduled task. Commands:

  python pr_tools.py discover --out runs/2026-11-15/leads.json
      Serper Google-News search (past 24h) for life-sciences M&A / VC / Commercial / IPO
      headlines. Raw leads only - Claude curates them into deals.json.

  python pr_tools.py extract runs/2026-11-15/deals.json
      For every deal: Serper search -> rank PR candidates -> Firecrawl scrape.
      Writes ma.json / vc.json / commercial.json / ipo.json next to deals.json
      (one row per deal, columns = COLUMNS below) and updates seen_deals.json.

  python pr_tools.py search "query" [--news]     one Serper search, prints results
  python pr_tools.py scrape <url>                one Firecrawl scrape, prints text

Keys come from the environment or from .env beside this file:
  SERPER_API_KEY=...
  FIRECRAWL_API_KEY=...
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen_deals.json"
MAX_CHARS = 49000  # Google Sheets cell limit is 50,000
MAX_TRIES = 3      # PR candidates scraped per deal before giving up

DEAL_TYPES = {"M&A": "ma", "VC": "vc", "Commercial": "commercial", "IPO": "ipo"}
COLUMNS = ["date_found", "deal_type", "deal_key", "company", "counterparty", "deal_summary",
           "amount", "announced_date", "source_url", "pr_link", "pr_text", "pr_status"]

# pr_link may ONLY come from the deal companies' own websites (top priority) or from these
# copyright-free release sources. Value = ranking score (higher is tried first).
ALLOWED_SOURCES = {
    "globenewswire.com": 30, "prnewswire.com": 29, "businesswire.com": 28,
    "accesswire.com": 27, "accessnewswire.com": 27,
    "cision.com": 26, "newswire.ca": 26, "prweb.com": 26,
    "finsmes.com": 20, "einpresswire.com": 18, "newsfilecorp.com": 18, "prlog.org": 16,
}
COMPANY_SITE_SCORE = 40
NEWSWIRES = list(ALLOWED_SOURCES)
STOP = {"inc", "incorporated", "corp", "corporation", "ltd", "limited", "llc", "lp", "llp", "plc",
        "co", "company", "the", "holdings", "group", "sa", "ag", "gmbh", "bv", "nv"}

SECTOR = '(biotech OR pharma OR "life sciences" OR healthcare OR medtech OR "digital health" OR diagnostics)'
DISCOVER_QUERIES = {
    "M&A": [f"{SECTOR} acquires", f"{SECTOR} \"to acquire\"", f"{SECTOR} acquisition completed",
            f"{SECTOR} merger agreement", "biopharma buyout definitive agreement"],
    "VC": [f"{SECTOR} \"Series A\"", f"{SECTOR} \"Series B\"", f"{SECTOR} \"Series C\" OR \"Series D\"",
           f"{SECTOR} seed financing raises", f"{SECTOR} raises million funding round",
           "biotech closes financing oversubscribed"],
    "Commercial": [f"{SECTOR} licensing agreement", f"{SECTOR} collaboration agreement",
                   f"{SECTOR} strategic partnership announces", "pharma option license upfront milestone",
                   f"{SECTOR} co-development commercialization agreement", "biotech supply distribution agreement"],
    "IPO": [f"{SECTOR} IPO pricing", f"{SECTOR} \"initial public offering\"", f"{SECTOR} files for IPO",
            "biotech Nasdaq debut", f"{SECTOR} SPAC business combination"],
}


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def key(name):
    v = os.environ.get(name, "")
    if not v:
        sys.exit(f"Missing {name}. Put it in {HERE / '.env'}")
    return v


# ---------------------------------------------------------------- Serper
def serper(query, news=False, num=10, past_day=False):
    body = {"q": query, "num": num}
    if past_day:
        body["tbs"] = "qdr:d"
    url = "https://google.serper.dev/" + ("news" if news else "search")
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=30,
                              headers={"X-API-KEY": key("SERPER_API_KEY"), "Content-Type": "application/json"})
            if r.status_code == 200:
                return r.json().get("news" if news else "organic", []) or []
            if r.status_code in (401, 403):
                sys.exit(f"Serper rejected the key ({r.status_code}): {r.text[:200]}")
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return []


# ---------------------------------------------------------------- Firecrawl
JUNK = re.compile(r"(enable (javascript|cookies)|accept (all )?cookies|cookie (policy|settings|consent)|"
                  r"create (an|your) account|sign in to continue|log in to continue|verify you are human|"
                  r"are you a robot|access denied|403 forbidden|subscribe to (read|continue)|"
                  r"javascript is (disabled|required))", re.I)
NOT_FOUND = ["page not found", "uh oh", "try retracing your steps", "doesn't exist", "has been moved",
             "we do not support this site"]


def firecrawl(url):
    """Returns (text, error). text is '' when the page is not a usable press release."""
    try:
        r = requests.post("https://api.firecrawl.dev/v1/scrape", timeout=120,
                          headers={"Authorization": "Bearer " + key("FIRECRAWL_API_KEY"),
                                   "Content-Type": "application/json"},
                          json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
    except requests.RequestException as e:
        return "", f"request failed: {e}"
    if r.status_code == 402:
        return "", "firecrawl credits exhausted (402)"
    if r.status_code != 200:
        return "", f"firecrawl http {r.status_code}"
    data = r.json()
    if data.get("success") is False:
        return "", "firecrawl: " + str(data.get("error", "failed"))[:150]
    text = ((data.get("data") or {}).get("markdown") or "").strip()
    preview = text[:500].lower()
    if any(s in preview for s in NOT_FOUND):
        return "", "page not found"
    if len(text) < 1500 and JUNK.search(text):
        return "", "consent/login wall"
    if len(text) < 300:
        return "", "page too short"
    return text[:MAX_CHARS], ""


# ---------------------------------------------------------------- PR candidate ranking
def host(url):
    m = re.match(r"^https?://([^/?#]+)", url or "", re.I)
    return re.sub(r"^www\.", "", m.group(1)).lower() if m else ""


def norm(name):
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return "".join(w for w in words if w not in STOP)


def source_score(h):
    """Score of an allowed release source, 0 when the host is not on the list."""
    return max([v for d, v in ALLOWED_SOURCES.items() if h == d or h.endswith("." + d)] or [0])


def age_score(url):
    """Newswire URLs carry the release date; old releases of the same company are not this deal."""
    m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url) or re.search(r"/(20\d\d)(\d\d)(\d\d)\d{6}/", url)
    if not m:
        return 0
    try:
        age = (dt.date.today() - dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
    except ValueError:
        return 0
    return 8 if age <= 10 else (-5 if age <= 45 else -60)


def is_company_domain(h, nn):
    parts = h.split(".")
    c = parts[-2] if len(parts) >= 2 else h
    if not nn or len(c) < 4:
        return False
    return c == nn or (len(nn) >= 5 and nn in c) or (len(c) >= 5 and c in nn)


def valid_article(url, h):
    low = url.lower()
    if h.endswith("prnewswire.com") and not low.endswith(".html"):
        return False
    if re.search(r"/(category|tag|tags|topics?|author|search)/|sitemap", low):
        return False
    return len(re.sub(r"^https?://[^/]+", "", low).strip("/")) > 0  # not a bare homepage


def build_queries(d):
    t, co, cp = d["deal_type"], d.get("company", "").strip(), d.get("counterparty", "").strip()
    amt = d.get("amount", "").strip()
    q = lambda s: f'"{s}"' if s else ""
    word = {"M&A": "acquisition", "VC": "funding", "Commercial": "agreement", "IPO": "initial public offering"}[t]
    main = " ".join(x for x in [q(co), q(cp) if t != "VC" else "", amt if t in ("VC", "IPO") else "", word] if x)
    sites = " OR ".join("site:" + s for s in NEWSWIRES[:8])
    wire = " ".join(x for x in [q(co), q(cp) if t in ("M&A", "Commercial") else "", f"({sites})", word] if x)
    news = " ".join(x for x in [q(co), q(cp) if t != "VC" else "", word] if x)
    return main, wire, news


def candidates(d):
    main, wire, news = build_queries(d)
    names = [norm(d.get("company")), norm(d.get("counterparty"))]
    title_words = [w for w in re.findall(r"[a-z0-9]+", (d.get("company", "") or "").lower()) if w not in STOP]
    out, seen = [], set()

    def add(url, title, score):
        url = (url or "").strip()
        h = host(url)
        if not url or url in seen or not h or not valid_article(url, h):
            return
        if any(is_company_domain(h, n) for n in names if n):
            score += COMPANY_SITE_SCORE          # the company's own website comes first
        elif source_score(h):
            score += source_score(h)
        else:
            return                               # not an allowed source: never used as pr_link
        score += age_score(url)
        seen.add(url)
        if title_words and all(w in (title or "").lower() for w in title_words[:2]):
            score += 10
        out.append({"url": url, "title": title or "", "score": score})

    if d.get("pr_link_hint"):
        add(d["pr_link_hint"], d.get("company", ""), 6)
    for r in serper(wire):
        add(r.get("link"), r.get("title"), 5)
    for r in serper(main):
        add(r.get("link"), r.get("title"), 3)
    for r in serper(news, news=True):
        add(r.get("link"), r.get("title"), 0)
    if d.get("source_url"):
        add(d["source_url"], d.get("company", ""), 1)   # kept only if it is itself an allowed source
    out.sort(key=lambda c: -c["score"])
    return out


def deal_key(d):
    return "|".join([DEAL_TYPES[d["deal_type"]], norm(d.get("company")), norm(d.get("counterparty"))])


# ---------------------------------------------------------------- commands
def cmd_discover(a):
    leads, seen = [], set()
    for t, queries in DISCOVER_QUERIES.items():
        for q in queries:
            for r in serper(q, news=True, num=20, past_day=True):
                link = r.get("link", "")
                if link and link not in seen:
                    seen.add(link)
                    leads.append({"type_hint": t, "title": r.get("title", ""), "snippet": r.get("snippet", ""),
                                  "source": r.get("source", ""), "date": r.get("date", ""), "link": link})
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(leads, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(leads)} leads -> {out}")
    for t in DISCOVER_QUERIES:
        print(f"  {t}: {sum(1 for l in leads if l['type_hint'] == t)}")


def cmd_extract(a):
    src = Path(a.deals)
    deals = json.loads(src.read_text(encoding="utf-8"))
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    today = dt.date.today().isoformat()
    rows = {t: [] for t in DEAL_TYPES}
    skipped = 0
    for d in deals:
        if d.get("deal_type") not in DEAL_TYPES:
            print(f"  ! unknown deal_type {d.get('deal_type')!r}: {d.get('company')}")
            continue
        k = deal_key(d)
        if k in seen and not a.force:
            skipped += 1
            continue
        cands = candidates(d)
        link, text, status, tried = "", "", "no_pr_found", []
        for c in cands[:MAX_TRIES]:
            text, err = firecrawl(c["url"])
            tried.append(f"{c['url']} -> {err or 'ok'}")
            if text:
                link, status = c["url"], "ok"
                break
            if not link:
                link, status = c["url"], "scrape_failed"   # keep best link even if text failed
        row = {"date_found": today, "deal_type": d["deal_type"], "deal_key": k,
               "company": d.get("company", ""), "counterparty": d.get("counterparty", ""),
               "deal_summary": d.get("deal_summary", ""), "amount": d.get("amount", ""),
               "announced_date": d.get("announced_date", ""), "source_url": d.get("source_url", ""),
               "pr_link": link, "pr_text": text, "pr_status": status, "_tried": tried}
        rows[d["deal_type"]].append(row)
        seen[k] = today
        print(f"  [{status:13}] {d['deal_type']:10} {d.get('company', '')[:40]:40} {link[:70]}")
    for t, slug in DEAL_TYPES.items():
        (src.parent / f"{slug}.json").write_text(json.dumps(rows[t], indent=1, ensure_ascii=False), encoding="utf-8")
    if not a.dry_run:
        SEEN_FILE.write_text(json.dumps(seen, indent=1), encoding="utf-8")
    total = sum(len(v) for v in rows.values())
    ok = sum(1 for v in rows.values() for r in v if r["pr_status"] == "ok")
    print(f"{total} new deals ({ok} with PR text), {skipped} already seen -> {src.parent}")


def cmd_search(a):
    for r in serper(a.query, news=a.news):
        tag = "ALLOWED" if source_score(host(r.get("link", ""))) else "only if company's own site"
        print(f"- [{tag}] {r.get('title', '')}\n  {r.get('link', '')}\n  {r.get('snippet', '')[:200]}")


def cmd_scrape(a):
    text, err = firecrawl(a.url)
    print(text if text else f"FAILED: {err}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("discover"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("extract"); s.add_argument("deals")
    s.add_argument("--force", action="store_true", help="re-process deals already in seen_deals.json")
    s.add_argument("--dry-run", action="store_true", help="do not update seen_deals.json")
    s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--news", action="store_true"); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("scrape"); s.add_argument("url"); s.set_defaults(fn=cmd_scrape)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
