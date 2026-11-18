# AXLMED daily deals research (cloud routine, 7:00 PM IST)

You are running AXLMED's daily deals research in a cloud session. Work in the repo root (this folder): it has the helper script `pr_tools.py`. Run everything without asking questions; nobody is watching. Do not commit, push or open pull requests. Never print API keys.

The keys come from the environment variables `SERPER_API_KEY` and `FIRECRAWL_API_KEY` (pr_tools.py reads them automatically). If one is missing, say so in the final report.

Let TODAY = today's date in IST (Asia/Kolkata) as YYYY-MM-DD, and RUN = `runs/TODAY` inside the repo.

## STEP 0 - Setup
- `pip install -r requirements.txt` (quietly).
- Load the Google Sheets connector tools with ToolSearch (search "google sheets get_values"; you need get_spreadsheet, get_values, update_spreadsheet).
- Rebuild the list of deals already handed over, because this cloud session starts empty: read column C (deal_key) and column A (date_found) of the tabs "Daily M&A", "Daily VC", "Daily Commercial" and "Daily IPO" of spreadsheet "Deals Automation" (id `1t1g-KNIdYoxsCpkPHo_HLYdH_upwQUZ7ey97rq2AW2c`). Write them to `seen_deals.json` as a JSON object `{"<deal_key>": "<date_found>", ...}` (skip the header row and empty keys; a missing tab simply adds nothing). `pr_tools.py extract` then skips those deals.

## STEP 1 - Find leads
Run: `python pr_tools.py discover --out runs/TODAY/leads.json`
This writes past-24h Google News headlines (via Serper) with a type_hint. Read the file. Then add your own WebSearch queries to catch deals the leads missed (e.g. "biotech acquisition announced today", "Series A biotech raises", "pharma licensing agreement announced", "healthcare IPO priced", plus Fierce Biotech, Endpoints, BioSpace, STAT, MedTech Dive, FinSMEs deal coverage). If discover fails because of a key error, continue with WebSearch only and say so in the final report.

## STEP 2 - Curate the deals
Keep only deals that satisfy ALL of:
- Sector: life sciences, healthcare, health, biosciences - biotech, pharma, medtech/devices, diagnostics, digital health, healthcare services, tools, CRO/CDMO, health-focused investors.
- Deal type is exactly one of:
  "M&A" (acquisitions, mergers, buyouts, asset purchases, majority stakes),
  "VC" (private financings: seed, Series A-F, venture, growth, private placements into private companies),
  "Commercial" (licensing, collaboration, partnership, co-development, option, supply/distribution/commercialization agreements),
  "IPO" (IPO filings, pricings, listings, SPAC/de-SPAC listings).
- Announced within roughly the last 24-36 hours. Check the date on the company's or newswire's release when a news article reports an older deal. Drop rumours, opinion pieces, market-size reports, clinical/regulatory news, regulatory approvals of earlier deals, follow-on public offerings, grants, and anything not a real announced deal.
Merge duplicate reports of the same deal. Do not invent details: leave a field "" when unknown.

Write `runs/TODAY/deals.json` as a JSON array, one object per deal:
```
{"deal_type": "M&A" | "VC" | "Commercial" | "IPO",
 "company": main company (M&A: target; VC: company raising; Commercial: licensor/seller of the asset or, if unclear, the smaller party; IPO: issuer),
 "counterparty": other party (M&A: acquirer; VC: lead investor(s); Commercial: partner/licensee; IPO: exchange),
 "deal_summary": one plain sentence,
 "amount": e.g. "$120M" or "",
 "announced_date": "YYYY-MM-DD" or "",
 "source_url": the article where you found it,
 "pr_link_hint": the official press release URL if you already saw it, else ""}
```

## STEP 3 - PR link and PR text (Serper + Firecrawl)
Run: `python pr_tools.py extract runs/TODAY/deals.json`
It skips deals already in seen_deals.json, searches Serper for the press release, scrapes the text with Firecrawl, and writes ma.json, vc.json, commercial.json, ipo.json in RUN. Each row has: date_found, deal_type, deal_key, company, counterparty, deal_summary, amount, announced_date, source_url, pr_link, pr_text, pr_status (ok / scrape_failed / no_pr_found).

For rows that are not "ok", try once to fix them: `python pr_tools.py search "<query>"` to find the real press release, `python pr_tools.py scrape <url>` to get its text, and update the row in the json file (pr_link, pr_text capped at 49000 chars, pr_status "ok"). Spot-check that each ok row's pr_text really is about that deal (and is this deal, not an older release from the same company); if not, clear pr_text and set pr_status "scrape_failed". PR link and PR text must come only from Serper and Firecrawl via this script - do not paste text from WebFetch/WebSearch.

PR SOURCE RULE (strict): pr_link may only be (a) a page on the website of one of the deal's own companies - this is the first choice - or (b) a copyright-free release source: GlobeNewswire, PR Newswire, Business Wire, Accesswire / ACCESS Newswire, Cision (cision.com, newswire.ca, prweb.com), FinSMEs, EIN Presswire, Newsfile. Never use news sites, trade press, blogs, aggregators or stock sites (Fierce, Endpoints, BioSpace, STAT, Yahoo, TradingView, Reuters, SEC copies, etc.) as pr_link, even when they contain the full story. The script already enforces this; in `search` output only use results tagged [ALLOWED] or pages on the company's own domain. If no allowed source has the release, leave pr_link and pr_text empty with pr_status "no_pr_found".

## STEP 4 - Hand each deal type to its own n8n workflow via Google Sheets
Spreadsheet "Deals Automation", id `1t1g-KNIdYoxsCpkPHo_HLYdH_upwQUZ7ey97rq2AW2c`. Tabs: "Daily M&A" (ma.json), "Daily VC" (vc.json), "Daily Commercial" (commercial.json), "Daily IPO" (ipo.json). The n8n workflows read these tabs at 8:30 PM IST. For each tab:
- If the tab does not exist, create it and write this header row in row 1: date_found, deal_type, deal_key, company, counterparty, deal_summary, amount, announced_date, source_url, pr_link, pr_text, pr_status
- Read column C (deal_key) and skip rows whose deal_key is already there.
- Append the remaining rows below the last used row, columns in the header order, values written as raw text (use update_spreadsheet with updateCells and stringValue so dates stay YYYY-MM-DD and Sheets does not reformat anything). Do not include the "_tried" field. Never modify or delete existing rows or any other tab.
- Read the tab back to confirm the row count grew by the number appended.
If the Google Sheets tools are not available, do not use any other route: say clearly in the report that the sheet was NOT updated, and include the deals (type, company, counterparty, amount, pr_link) in the report so nothing is lost.

## STEP 5 - Report
Finish with a short summary: deals found per type, how many have PR text / no PR, rows appended per tab, and any failures (Serper key, Firecrawl credits, sheet access) with the exact error.
