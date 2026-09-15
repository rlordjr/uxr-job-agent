"""Maintains a rolling history dataset consumed by the GitHub Pages dashboard.

Each run appends today's top matches (from output/results.json) to
docs/data/history.json as one "run" entry. Old runs beyond HISTORY_RETENTION_DAYS
are dropped to keep the file small. The dashboard (docs/index.html) reads this
file client-side and computes its own "last 7 days, deduped, top 20" view.
"""

from datetime import datetime, timedelta
import json
import os

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
RESULTS_PATH = os.path.join(OUTPUT_DIR, "results.json")
DOCS_DATA_DIR = os.path.join(os.path.dirname(__file__), "docs", "data")
HISTORY_PATH = os.path.join(DOCS_DATA_DIR, "history.json")

HISTORY_RETENTION_DAYS = 30


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def main():
    today_results = load_json(RESULTS_PATH, [])
    history = load_json(HISTORY_PATH, {"runs": []})

    run_date = datetime.now().strftime("%Y-%m-%d")

    # Trim each job record down to only what the dashboard needs (keep the file lean).
    trimmed_jobs = []
    for job in today_results:
        trimmed_jobs.append({
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "region": job.get("region"),
            "posted_at": job.get("posted_at"),
            "match_score": job.get("match_score"),
            "fit_tier": job.get("fit_tier"),
            "url": job.get("url"),
            "signals": job.get("signals", {}),
        })

    runs = [r for r in history.get("runs", []) if r.get("run_date") != run_date]
    runs.append({"run_date": run_date, "jobs": trimmed_jobs})

    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).strftime("%Y-%m-%d")
    runs = [r for r in runs if r.get("run_date", "") >= cutoff]
    runs.sort(key=lambda r: r.get("run_date", ""))

    save_json(HISTORY_PATH, {"runs": runs, "last_updated": datetime.now().isoformat()})
    print(f"[INFO] Dashboard history updated: {len(runs)} run(s) retained, {len(trimmed_jobs)} jobs added for {run_date}.")


if __name__ == "__main__":
    main()
