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
    "united states", "usa", "u.s.", "remote - us", "remote, us",
    "remote - united states", "remote (us)", "remote (united states)",
    "san francisco", "new york", "austin", "seattle", "atlanta", "chicago",
    "denver", "los angeles", "boston"
]

# Standalone 2-letter codes need word-boundary matching, handled separately from US_LOCATIONS.
US_STATE_CODES = ["ca", "ny", "ga", "tx", "wa", "co", "il", "ma", "us"]

CANADA_CARIBBEAN_LOCATIONS = [
    "canada", "toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary",
    "ontario", "quebec", "québec", "british columbia", "alberta", "manitoba",
    "jamaica", "bahamas", "puerto rico", "dominican republic", "trinidad and tobago",
    "trinidad", "tobago", "barbados", "cuba", "haiti", "aruba", "curacao", "curaçao",
    "caribbean", "bermuda", "cayman islands"
]

MEXICO_SOUTH_AMERICA_LOCATIONS = [
    "mexico", "méxico", "mexico city", "cdmx", "guadalajara", "monterrey",
    "brazil", "brasil", "sao paulo", "são paulo", "rio de janeiro",
    "argentina", "buenos aires", "chile", "santiago",
    "colombia", "bogota", "bogotá", "medellin", "medellín",
    "peru", "perú", "lima", "ecuador", "quito", "uruguay", "montevideo",
    "paraguay", "asuncion", "asunción", "bolivia", "la paz", "venezuela", "caracas",
    "south america", "latam", "latin america"
]

EUROPE_LOCATIONS = [
    "europe", "emea", "uk", "united kingdom", "london", "manchester", "ireland",
    "dublin", "germany", "berlin", "munich", "france", "paris", "netherlands",
    "amsterdam", "spain", "madrid", "barcelona", "italy", "rome", "milan",
    "portugal", "lisbon", "poland", "warsaw", "sweden", "stockholm", "denmark",
    "copenhagen", "norway", "oslo", "finland", "helsinki", "switzerland", "zurich",
    "austria", "vienna", "belgium", "brussels", "romania", "bucharest", "czech",
    "prague", "hungary", "budapest", "greece", "athens"
]

ASIA_LOCATIONS = [
    "asia", "apac", "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad",
    "pune", "singapore", "japan", "tokyo", "china", "beijing", "shanghai",
    "shenzhen", "hong kong", "korea", "seoul", "philippines", "manila",
    "vietnam", "hanoi", "ho chi minh", "indonesia", "jakarta", "malaysia",
    "kuala lumpur", "thailand", "bangkok", "taiwan", "taipei", "pakistan",
    "bangladesh", "sri lanka", "israel", "tel aviv", "uae", "dubai", "abu dhabi",
    "saudi arabia"
]

OTHER_REGION_LOCATIONS = [
    "australia", "sydney", "melbourne", "new zealand", "auckland",
    "africa", "south africa", "cape town", "johannesburg", "nigeria", "lagos",
    "kenya", "nairobi", "egypt", "cairo", "oceania"
]


def classify_region(location: str) -> str:
    """Classify a job location into a coarse region bucket.

    Returns one of: 'united_states', 'canada_caribbean', 'mexico_south_america',
    'europe', 'asia', 'other', 'unknown'.
    """
    clean_loc = normalize_text(location)
    if not clean_loc:
        return "unknown"

    tokens = re.findall(r"[a-z]+", clean_loc)

    if any(loc in clean_loc for loc in US_LOCATIONS) or any(code in tokens for code in US_STATE_CODES):
        return "united_states"
    if any(loc in clean_loc for loc in CANADA_CARIBBEAN_LOCATIONS):
        return "canada_caribbean"
    if any(loc in clean_loc for loc in MEXICO_SOUTH_AMERICA_LOCATIONS):
        return "mexico_south_america"
    if any(loc in clean_loc for loc in EUROPE_LOCATIONS):
        return "europe"
    if any(loc in clean_loc for loc in ASIA_LOCATIONS):
        return "asia"
    if any(loc in clean_loc for loc in OTHER_REGION_LOCATIONS):
        return "other"
    if "remote" in clean_loc:
        return "unknown"  # Remote with no discernible country/region
    return "unknown"


ALLOWED_REGIONS = {"united_states", "canada_caribbean", "mexico_south_america"}

# Maps an explicit source country hint (e.g. Adzuna's known request country) to our region buckets,
# bypassing unreliable text-guessing for granular locations like "Mableton, Cobb County" with no
# state/country name that classify_region() can't recognize.
COUNTRY_CODE_TO_REGION = {
    "us": "united_states",
    "ca": "canada_caribbean",
    "mx": "mexico_south_america",
}


def resolve_region(location: str, source_country: str = None) -> str:
    if source_country:
        mapped = COUNTRY_CODE_TO_REGION.get(source_country.lower())
        if mapped:
            return mapped
    return classify_region(location)


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
    return classify_region(location) == "united_states"


def _find_matched_keywords(text: str, keywords: List[str]) -> List[str]:
    clean = normalize_text(text)
    matched = []
    for kw in keywords:
        if kw.lower() in clean:
            matched.append(kw)
    return matched


def score_job(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    title = _coalesce(job.get("title"), "")
    description = _coalesce(job.get("description"), "")
    company = _coalesce(job.get("company"), "")
    location = _coalesce(job.get("location"), "")
    posted_at = _coalesce(job.get("posted_at"), "")
    source_country = job.get("source_country")
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
        role_alignment_detail = "Direct title match to target UX research role"
    elif "researcher" in clean_title and any(d in clean_title for d in ["product", "experience", "design", "consumer"]):
        role_alignment_score = 90
        role_alignment_detail = "Strong product/experience research title match"
    elif "director, research" in clean_title or "head of research" in clean_title or "research manager" in clean_title:
        role_alignment_score = 90
        role_alignment_detail = "Research leadership title match"
    elif any(term in normalize_text(description) for term in primary_uxr_titles):
        role_alignment_score = 75
        role_alignment_detail = "UX research responsibilities described in role content"
    elif "research" in clean_title:
        role_alignment_score = 60
        role_alignment_detail = "General research title"
    else:
        role_alignment_score = 25
        role_alignment_detail = "Low direct title alignment"

    # 2. Seniority Fit (20%)
    seniority_tokens = ["senior", "sr", "lead", "principal", "staff", "manager", "director", "head", "vp"]
    junior_tokens = ["junior", "associate", "intern", "apprentice", "entry"]
    matched_seniority = [tok for tok in seniority_tokens if tok in clean_title]
    if any(token in clean_title for token in junior_tokens):
        seniority_score = 20
        seniority_detail = "Entry-level or intern level"
    elif matched_seniority:
        seniority_score = 100
        seniority_detail = f"Seniority level: {', '.join(matched_seniority).title()}"
    elif "researcher" in clean_title:
        seniority_score = 75
        seniority_detail = "Mid/Senior researcher level"
    else:
        seniority_score = 50
        seniority_detail = "Unspecified seniority"

    # 3. Methods and Skills Fit (15%)
    method_keywords = [
        "interview", "interviews", "qualitative", "usability", "moderated", "unmoderated",
        "synthesis", "workshop", "workshops", "mixed methods", "diary study", "field study",
        "ethnography", "heuristic", "discovery", "user journey", "enablement", "research ops",
        "ai-assisted", "generative"
    ]
    matched_methods = _find_matched_keywords(text_blob, method_keywords)
    methods_score = min(100, int((len(matched_methods) / 4) * 100))
    methods_detail = f"Matched methods: {', '.join(matched_methods[:5])}" if matched_methods else "No specific qualitative methods highlighted"

    # 4. Strategic Influence & Leadership Fit (15%)
    strategic_keywords = [
        "stakeholder", "strategy", "leadership", "coaching", "mentoring", "influence",
        "cross-functional", "prioritization", "partner", "roadmap", "decision", "business impact"
    ]
    matched_leadership = _find_matched_keywords(text_blob, strategic_keywords)
    strategic_score = min(100, int((len(matched_leadership) / 4) * 100))
    leadership_detail = f"Leadership signals: {', '.join(matched_leadership[:5])}" if matched_leadership else "Standard individual contributor scope"

    # 5. Industry / Domain Fit (10%)
    industry_terms = [
        "financial", "fintech", "real estate", "logistics", "enterprise", "saas",
        "b2b", "consulting", "consumer", "platform"
    ]
    matched_industries = _find_matched_keywords(text_blob, industry_terms)
    industry_score = min(100, max(70, int(70 + (len(matched_industries) * 10))))

    # 6. Work Arrangement & Geography Fit (5%) — priority: US > Canada/Caribbean > Mexico/South America
    region = resolve_region(location, source_country)
    is_remote = "remote" in normalize_text(location) or "remote" in normalize_text(text_blob)

    region_cap = None
    if region == "united_states":
        work_score = 100 if is_remote else 85
    elif region == "canada_caribbean":
        work_score = 60 if is_remote else 50
        region_cap = 75.0
    elif region == "mexico_south_america":
        work_score = 30 if is_remote else 25
        region_cap = 60.0
    else:
        # Europe, Asia, or other/unknown regions are outside the target geography.
        work_score = 0
        region_cap = 65.0

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

    # Cap score by region priority tier to prevent lower-priority regions from outranking the US.
    if region_cap is not None:
        weighted_total = min(weighted_total, region_cap)

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
        "region": region,
        "posted_at": posted_at,
        "match_score": total,
        "category_scores": category_scores,
        "fit_tier": fit_tier,
        "signals": {
            "role": role_alignment_detail,
            "seniority": seniority_detail,
            "methods": methods_detail,
            "leadership": leadership_detail,
            "matched_methods_list": matched_methods,
            "matched_leadership_list": matched_leadership,
        }
    }
