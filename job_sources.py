import html
import json
import re
from typing import Any, Dict, List

import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ralph-job-agent/1.0; +https://example.com)"
}


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    text = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", text).strip()


def fetch_greenhouse_jobs(company_slug: str, company_name: str) -> List[Dict[str, Any]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
    response = requests.get(url, headers=HEADERS, timeout=30)
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
    response = requests.get(url, headers=HEADERS, timeout=30)
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
    response = requests.get(url, headers=HEADERS, timeout=30)
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


def fetch_jobs_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    all_jobs: List[Dict[str, Any]] = []
    for source in config.get("job_sources", []):
        source_type = source.get("type", "").lower()
        company_slug = source.get("company")
        company_name = source.get("name") or company_slug
        try:
            if source_type == "greenhouse":
                jobs = fetch_greenhouse_jobs(company_slug, company_name)
            elif source_type == "lever":
                jobs = fetch_lever_jobs(company_slug, company_name)
            elif source_type == "ashby":
                jobs = fetch_ashby_jobs(company_slug, company_name)
            else:
                jobs = []
        except Exception as exc:
            print(f"[WARN] Failed to fetch {company_name} ({source_type}): {exc}")
            continue
        all_jobs.extend(jobs)

    return all_jobs
