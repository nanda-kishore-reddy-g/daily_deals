# daily_deals

AXLMED daily life-sciences deals research. A Claude cloud routine runs every day at 7:00 PM IST, follows `TASK.md`, and appends the day's M&A, VC, Commercial and IPO deals (with press-release link and text) to the "Daily ..." tabs of the Deals Automation Google Sheet. The n8n workflows read those tabs at 8:30 PM IST.

- `pr_tools.py`: Serper (find deals and press releases) and Firecrawl (press-release text). Keys come from the environment variables `SERPER_API_KEY` and `FIRECRAWL_API_KEY`, or from a local `.env` file (never committed).
- `TASK.md`: the instructions the routine follows. Edit it here to change the daily task.

Not in the repo (see `.gitignore`): `.env`, `runs/`, `seen_deals.json`, `n8n/` (the n8n workflow files contain an API key).
