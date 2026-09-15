import concurrent.futures
import html
import json
import os
import re
from typing import Any, Dict, List

import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ralph-job-agent/1.0; +https://example.com)"
}

# Adzuna covers these countries for our target regions (Caribbean/most of South America
# aren't in Adzuna's supported country list, so US/Canada/Mexico give the best available reach).
ADZUNA_COUNTRIES = ["us", "ca", "mx"]
ADZUNA_RESULTS_PER_PAGE = 50
ADZUNA_MAX_PAGES = 2


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    text = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", text).strip()


def fetch_greenhouse_jobs(company_slug: str, company_name: str) -> List[Dict[str, Any]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
    response = requests.get(url, headers=HEADERS, timeout=8)
    response.raise_for_status()
    payload = response.json()

    jobs = []
    for item in payload.get("jobs", []):
        title = item.get("title")
        location = item.get("location")
        if isinstance(location, dict):
            location_name = location.get("name") or "Unknown"
        else:
            location_name = str(location or "Unknown")

        raw_desc = item.get("content") or item.get("description") or ""
        clean_desc = clean_html(raw_desc)

        jobs.append({
            "title": title,
            "company": company_name,
            "location": location_name,
            "description": clean_desc,
            "source": "greenhouse",
            "url": item.get("absolute_url") or f"https://boards.greenhouse.io/{company_slug}/jobs/{item.get('id')}",
            "salary": item.get("salary") or item.get("compensation") or "",
            "posted_at": item.get("updated_at") or "",
            "raw": item,
        })
    return jobs


def fetch_lever_jobs(company_slug: str, company_name: str) -> List[Dict[str, Any]]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    response = requests.get(url, headers=HEADERS, timeout=8)
    response.raise_for_status()
    payload = response.json()

    jobs = []
    for item in payload:
        location = item.get("categories", {}).get("location") or item.get("location") or "Unknown"
        location_name = str(location) if location else "Unknown"
        raw_desc = item.get("description") or item.get("descriptionPlain") or ""
        clean_desc = clean_html(raw_desc)

        jobs.append({
            "title": item.get("text"),
            "company": company_name,
            "location": location_name,
            "description": clean_desc,
            "source": "lever",
            "url": item.get("hostedUrl") or item.get("applyUrl") or "",
            "salary": item.get("salary") or "",
            "posted_at": item.get("createdAt") or "",
            "raw": item,
        })
    return jobs


def fetch_ashby_jobs(company_slug: str, company_name: str) -> List[Dict[str, Any]]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
    response = requests.get(url, headers=HEADERS, timeout=8)
    response.raise_for_status()
    payload = response.json()

    jobs = []
    for item in payload.get("jobs", []):
        location = item.get("location") or "Unknown"
        sec_locations = item.get("secondaryLocations") or []
        loc_names = [location] + [loc.get("location", "") if isinstance(loc, dict) else str(loc) for loc in sec_locations]
        full_location = " / ".join([loc for loc in loc_names if loc])

        raw_desc = item.get("descriptionHtml") or item.get("descriptionPlain") or ""
        clean_desc = clean_html(raw_desc)

        jobs.append({
            "title": item.get("title"),
            "company": company_name,
            "location": full_location or "Unknown",
            "description": clean_desc,
            "source": "ashby",
            "url": item.get("jobUrl") or f"https://jobs.ashbyhq.com/{company_slug}/{item.get('id')}",
            "salary": str(item.get("compensation") or ""),
            "posted_at": item.get("publishedAt") or "",
            "raw": item,
        })
    return jobs


def fetch_adzuna_jobs(country: str, query: str, app_id: str, app_key: str) -> List[Dict[str, Any]]:
    jobs = []
    for page in range(1, ADZUNA_MAX_PAGES + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": query,
            "results_per_page": ADZUNA_RESULTS_PER_PAGE,
            "content-type": "application/json",
        }
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if not results:
            break

        for item in results:
            company = (item.get("company") or {}).get("display_name") or "Unknown"
            location = (item.get("location") or {}).get("display_name") or "Unknown"
            description = item.get("description") or ""

            jobs.append({
                "title": item.get("title"),
                "company": company,
                "location": location,
                "description": description,
                "source": f"adzuna-{country}",
                "source_country": country,
                "url": item.get("redirect_url") or "",
                "salary": item.get("salary_min") or "",
                "posted_at": item.get("created") or "",
                "raw": item,
            })

        if len(results) < ADZUNA_RESULTS_PER_PAGE:
            break
    return jobs


def _fetch_adzuna_all(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("[INFO] Adzuna credentials not configured (ADZUNA_APP_ID/ADZUNA_APP_KEY); skipping aggregator search.")
        return []

    queries = config.get("adzuna_search_queries") or ["UX Researcher", "User Researcher", "Design Researcher"]
    tasks = [(country, query) for country in ADZUNA_COUNTRIES for query in queries]

    jobs: List[Dict[str, Any]] = []

    def _run(task):
        country, query = task
        try:
            return fetch_adzuna_jobs(country, query, app_id, app_key)
        except Exception as exc:
            print(f"[WARN] Failed to fetch Adzuna jobs ({country}, '{query}'): {exc}")
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for job_list in executor.map(_run, tasks):
            jobs.extend(job_list)

    return jobs


def _fetch_single_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    source_type = source.get("type", "").lower()
    company_slug = source.get("company")
    company_name = source.get("name") or company_slug
    try:
        if source_type == "greenhouse":
            return fetch_greenhouse_jobs(company_slug, company_name)
        elif source_type == "lever":
            return fetch_lever_jobs(company_slug, company_name)
        elif source_type == "ashby":
            return fetch_ashby_jobs(company_slug, company_name)
        return []
    except Exception as exc:
        print(f"[WARN] Failed to fetch {company_name} ({source_type}): {exc}")
        return []


def fetch_jobs_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    all_jobs: List[Dict[str, Any]] = []
    sources = config.get("job_sources", [])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = executor.map(_fetch_single_source, sources)
        for job_list in results:
            all_jobs.extend(job_list)

    all_jobs.extend(_fetch_adzuna_all(config))

    return all_jobs
