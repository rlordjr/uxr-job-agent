import json
import re
from typing import Any, Dict, List


WEIGHTS = {
    "role_alignment": 30,
    "seniority_fit": 20,
    "methods_and_skills": 15,
    "strategic_influence": 15,
    "industry_fit": 10,
    "work_arrangement": 5,
    "compensation": 5,
}

US_LOCATIONS = [
    "united states", "usa", "us", "u.s.", "remote - us", "remote, us",
    "remote - united states", "remote (us)", "remote (united states)",
    "san francisco", "new york", "austin", "seattle", "atlanta", "chicago",
    "denver", "los angeles", "boston", "ca", "ny", "ga", "tx", "wa", "co", "il", "ma"
]

NON_US_LOCATIONS = [
    "india", "bengaluru", "bangalore", "london", "dublin", "germany", "berlin",
    "singapore", "sydney", "australia", "uk", "united kingdom", "ireland", "japan",
    "tokyo", "france", "paris", "netherlands", "amsterdam", "canada", "toronto", "vancouver", "ontario"
]


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def load_profile(profile_path: str) -> Dict[str, Any]:
    with open(profile_path, "r", encoding="utf-8") as f:
        return json.load(f)["candidate_profile"]


def _coalesce(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _contains_any(text: str, needles: List[str]) -> bool:
    clean = normalize_text(text)
    return any(needle.lower() in clean for needle in needles if needle)


def _count_keyword_hits(text: str, keywords: List[str]) -> int:
    clean = normalize_text(text)
    hits = 0
    for keyword in [k.lower() for k in keywords if k]:
        if keyword in clean:
            hits += 1
    return hits


def _match_score_for_category(score: float, weight: int) -> float:
    return round((score / 100) * weight, 2)


def is_us_location(location: str) -> bool:
    clean_loc = normalize_text(location)
    if not clean_loc:
        return True  # Give benefit of doubt if unknown

    # If explicitly non-US and no mention of US
    if any(non_us in clean_loc for non_us in NON_US_LOCATIONS) and not any(us in clean_loc for us in ["united states", "usa", "us"]):
        return False

    return any(us in clean_loc for us in US_LOCATIONS)


def score_job(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    title = _coalesce(job.get("title"), "")
    description = _coalesce(job.get("description"), "")
    company = _coalesce(job.get("company"), "")
    location = _coalesce(job.get("location"), "")
    text_blob = f"{title} {description} {company} {location}"
    clean_title = normalize_text(title)

    # 1. Role Alignment (30%)
    primary_uxr_titles = [
        "ux researcher", "user researcher", "user experience researcher",
        "design researcher", "qualitative researcher", "product researcher",
        "research ops", "research operations", "research enablement",
        "user insights", "experience researcher"
    ]
    if any(term in clean_title for term in primary_uxr_titles):
        role_alignment_score = 100
    elif "researcher" in clean_title and any(d in clean_title for d in ["product", "experience", "design", "consumer"]):
        role_alignment_score = 90
    elif "director, research" in clean_title or "head of research" in clean_title or "research manager" in clean_title:
        role_alignment_score = 90
    elif any(term in normalize_text(description) for term in primary_uxr_titles):
        role_alignment_score = 75
    elif "research" in clean_title:
        role_alignment_score = 60
    else:
        role_alignment_score = 25

    # 2. Seniority Fit (20%)
    seniority_tokens = ["senior", "sr", "lead", "principal", "staff", "manager", "director", "head", "vp"]
    junior_tokens = ["junior", "associate", "intern", "apprentice", "entry"]
    if any(token in clean_title for token in junior_tokens):
        seniority_score = 20
    elif any(token in clean_title for token in seniority_tokens):
        seniority_score = 100
    elif "researcher" in clean_title:
        seniority_score = 75
    else:
        seniority_score = 50

    # 3. Methods and Skills Fit (15%)
    method_keywords = [
        "interview", "interviews", "qualitative", "usability", "moderated", "unmoderated",
        "synthesis", "workshop", "workshops", "mixed methods", "diary study", "field study",
        "ethnography", "heuristic", "discovery", "user journey", "enablement", "research ops",
        "ai-assisted", "generative"
    ]
    method_hits = _count_keyword_hits(text_blob, method_keywords)
    methods_score = min(100, int((method_hits / 4) * 100))

    # 4. Strategic Influence & Leadership Fit (15%)
    strategic_keywords = [
        "stakeholder", "strategy", "leadership", "coaching", "mentoring", "influence",
        "cross-functional", "prioritization", "partner", "roadmap", "decision", "business impact"
    ]
    strategic_hits = _count_keyword_hits(text_blob, strategic_keywords)
    strategic_score = min(100, int((strategic_hits / 4) * 100))

    # 5. Industry / Domain Fit (10%)
    industry_terms = [
        "financial", "fintech", "real estate", "logistics", "enterprise", "saas",
        "b2b", "consulting", "consumer", "platform"
    ]
    industry_hits = _count_keyword_hits(text_blob, industry_terms)
    industry_score = min(100, max(70, int(70 + (industry_hits * 10))))

    # 6. Work Arrangement & Geography Fit (5%)
    is_us = is_us_location(location)
    is_remote = "remote" in normalize_text(location) or "remote" in normalize_text(text_blob)

    if not is_us:
        work_score = 10  # Heavy penalty for non-US when US-only is preferred
    elif is_remote:
        work_score = 100
    else:
        work_score = 85

    # 7. Compensation Fit (5%)
    compensation_score = 100

    category_scores = {
        "role_alignment": role_alignment_score,
        "seniority_fit": seniority_score,
        "methods_and_skills": methods_score,
        "strategic_influence": strategic_score,
        "industry_fit": industry_score,
        "work_arrangement": work_score,
        "compensation": compensation_score,
    }

    weighted_total = sum(_match_score_for_category(category_scores[name], WEIGHTS[name]) for name in WEIGHTS)
    total = round(weighted_total, 2)

    fit_tier = "Reject"
    if total >= 80:
        fit_tier = "Strong fit"
    elif total >= 68:
        fit_tier = "Good fit"
    elif total >= 55:
        fit_tier = "Marginal fit"

    return {
        "title": title,
        "company": company,
        "location": location,
        "match_score": total,
        "category_scores": category_scores,
        "fit_tier": fit_tier
    }
