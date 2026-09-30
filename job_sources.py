import concurrent.futures
import html
import json
import os
import re
import time
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
# Adzuna's default limit is 25 requests/minute (also 250/day, 1000/week, 2500/month).
# Requests run one at a time, spaced under that limit, and back off on 429.
ADZUNA_REQUEST_INTERVAL_SECONDS = 2.5
ADZUNA_MAX_RETRIES = 3
ADZUNA_RETRY_WAIT_SECONDS = 30

# JSearch (via OpenWebNinja) wraps Google for Jobs, which covers LinkedIn, Indeed, Glassdoor, etc.
# Each query costs one request against the monthly quota (200 on the free plan), so keep queries few.
JSEARCH_URL = "https://api.openwebninja.com/jsearch/search-v2"

USAJOBS_URL = "https://data.usajobs.gov/api/Search"
USAJOBS_MAX_DAYS = 60  # API limit for DatePosted


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
            # updated_at changes whenever a posting is edited; first_published is the real post date.
            "posted_at": item.get("first_published") or item.get("updated_at") or "",
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


def _adzuna_get(url: str, params: Dict[str, Any]) -> requests.Response:
    for attempt in range(ADZUNA_MAX_RETRIES + 1):
        time.sleep(ADZUNA_REQUEST_INTERVAL_SECONDS)
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if response.status_code != 429 or attempt == ADZUNA_MAX_RETRIES:
            response.raise_for_status()
            return response
        retry_after = response.headers.get("Retry-After", "")
        wait = int(retry_after) if retry_after.isdigit() else ADZUNA_RETRY_WAIT_SECONDS * (attempt + 1)
        print(f"[WARN] Adzuna rate limit hit; retrying in {wait}s (attempt {attempt + 1}/{ADZUNA_MAX_RETRIES}).")
        time.sleep(wait)


def fetch_adzuna_jobs(country: str, query: str, app_id: str, app_key: str, max_days_old: int = None) -> List[Dict[str, Any]]:
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
        if max_days_old:
            params["max_days_old"] = max_days_old
        payload = _adzuna_get(url, params).json()
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
    max_days_old = config.get("search_window_days")
    tasks = [(country, query) for country in ADZUNA_COUNTRIES for query in queries]

    jobs: List[Dict[str, Any]] = []
    for country, query in tasks:
        try:
            jobs.extend(fetch_adzuna_jobs(country, query, app_id, app_key, max_days_old))
        except Exception as exc:
            print(f"[WARN] Failed to fetch Adzuna jobs ({country}, '{query}'): {exc}")
    return jobs


def fetch_jsearch_jobs(query: str, api_key: str, date_posted: str) -> List[Dict[str, Any]]:
    params = {"query": query, "country": "us", "date_posted": date_posted}
    headers = {**HEADERS, "x-api-key": api_key}
    response = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    jobs = []
    for item in payload.get("data") or []:
        location = item.get("job_location") or ", ".join(
            part for part in [item.get("job_city"), item.get("job_state"), item.get("job_country")] if part
        )
        if item.get("job_is_remote"):
            location = f"Remote - {location}" if location else "Remote - US"

        jobs.append({
            "title": item.get("job_title"),
            "company": item.get("employer_name") or "Unknown",
            "location": location or "Unknown",
            "description": item.get("job_description") or "",
            "source": "jsearch",
            "source_country": (item.get("job_country") or "us").lower(),
            "url": item.get("job_apply_link") or item.get("job_google_link") or "",
            "salary": item.get("job_min_salary") or "",
            "posted_at": item.get("job_posted_at_datetime_utc") or item.get("job_posted_at_timestamp") or "",
            "raw": item,
        })
    return jobs


def _fetch_jsearch_all(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    api_key = os.getenv("JSEARCH_API_KEY")
    if not api_key:
        print("[INFO] JSearch credentials not configured (JSEARCH_API_KEY); skipping JSearch.")
        return []

    queries = config.get("jsearch_search_queries") or ["UX Researcher"]
    date_posted = config.get("jsearch_date_posted", "3days")

    jobs: List[Dict[str, Any]] = []
    for query in queries:
        try:
            jobs.extend(fetch_jsearch_jobs(query, api_key, date_posted))
        except Exception as exc:
            print(f"[WARN] Failed to fetch JSearch jobs ('{query}'): {exc}")
    return jobs


def fetch_usajobs_jobs(keyword: str, api_key: str, email: str, days: int) -> List[Dict[str, Any]]:
    params = {"Keyword": keyword, "DatePosted": days, "ResultsPerPage": 500}
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": api_key}
    response = requests.get(USAJOBS_URL, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    jobs = []
    for result in (payload.get("SearchResult") or {}).get("SearchResultItems") or []:
        item = result.get("MatchedObjectDescriptor") or {}
        details = (item.get("UserArea") or {}).get("Details") or {}
        pay = (item.get("PositionRemuneration") or [{}])[0]

        jobs.append({
            "title": item.get("PositionTitle"),
            "company": item.get("OrganizationName") or item.get("DepartmentName") or "US Federal Government",
            "location": item.get("PositionLocationDisplay") or "United States",
            "description": " ".join(part for part in [details.get("JobSummary"), item.get("QualificationSummary")] if part),
            "source": "usajobs",
            "source_country": "us",
            "url": item.get("PositionURI") or "",
            "salary": pay.get("MinimumRange") or "",
            "posted_at": item.get("PublicationStartDate") or "",
            "raw": item,
        })
    return jobs


def _fetch_usajobs_all(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    api_key = os.getenv("USA_JOBS")
    # USAJobs requires the email the key was registered with, sent as the User-Agent.
    email = os.getenv("USA_JOBS_EMAIL") or os.getenv("EMAIL_TO")
    if not api_key or not email:
        print("[INFO] USAJobs credentials not configured (USA_JOBS + EMAIL_TO); skipping USAJobs.")
        return []

    keywords = config.get("usajobs_search_queries") or ["user experience research"]
    days = min(int(config.get("search_window_days", 30)), USAJOBS_MAX_DAYS)

    jobs: List[Dict[str, Any]] = []
    for keyword in keywords:
        try:
            jobs.extend(fetch_usajobs_jobs(keyword, api_key, email, days))
        except Exception as exc:
            print(f"[WARN] Failed to fetch USAJobs jobs ('{keyword}'): {exc}")
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

    for source_name, fetch in [("Adzuna", _fetch_adzuna_all), ("JSearch", _fetch_jsearch_all), ("USAJobs", _fetch_usajobs_all)]:
        jobs = fetch(config)
        print(f"[INFO] {source_name}: fetched {len(jobs)} job(s).")
        all_jobs.extend(jobs)

    return all_jobs
