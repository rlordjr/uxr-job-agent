import json
import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List

from job_sources import fetch_jobs_from_config
from scorer import load_profile, score_job

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "candidate_profile.json")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
PREVIOUS_RESULTS_PATH = os.path.join(OUTPUT_DIR, "previous_jobs.json")
RESULTS_PATH = os.path.join(OUTPUT_DIR, "results.json")
NEW_JOBS_PATH = os.path.join(OUTPUT_DIR, "new_jobs.json")
TOP_MATCHES_PATH = os.path.join(OUTPUT_DIR, "top_matches.csv")


def ensure_output_dir() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dedupe_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique = []
    for job in jobs:
        key = (job.get("title") or "", job.get("company") or "", job.get("url") or "", job.get("location") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def is_target_role(job_title: str, config: Dict[str, Any]) -> bool:
    title = (job_title or "").strip().lower()
    if not title:
        return False

    exclude_keywords = config.get("role_exclude_keywords", [])
    include_keywords = config.get("role_include_keywords", [])
    target_roles = config.get("target_roles", [])

    # Check for hard excludes (e.g. AI research scientist, security research, marketing)
    for excl in exclude_keywords:
        if excl.lower() in title:
            # Only allow if explicitly marked as UX or User research
            if not any(ux_kw in title for ux_kw in ["ux research", "ux researcher", "user research", "user researcher", "design research", "qualitative research"]):
                return False

    # Check for direct target roles or include keywords match
    for role in target_roles:
        if role.lower() in title:
            return True

    for inc in include_keywords:
        if inc.lower() in title:
            return True

    # Combination match: must have (uxr / ux / user / design / qualitative) + (research / researcher / insights / ops)
    has_research_term = any(term in title for term in ["researcher", "research", "insights", "ops", "enablement"])
    has_domain_term = any(term in title for term in ["ux", "user", "design", "qualitative"])

    if has_research_term and has_domain_term:
        return True

    return False


def save_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def save_top_matches_csv(results: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("title,company,location,fit_tier,match_score,url\n")
        for item in results:
            f.write(
                f"{item.get('title','')},{item.get('company','')},{item.get('location','')},{item.get('fit_tier','')},{item.get('match_score','')},{item.get('url','')}\n"
            )


def load_previous_jobs() -> Dict[str, Any]:
    if os.path.exists(PREVIOUS_RESULTS_PATH):
        return load_json(PREVIOUS_RESULTS_PATH)
    return {"jobs": []}


def send_email_summary(summary: str, config: Dict[str, Any]) -> None:
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled"):
        print("[INFO] Email is disabled in config.json")
        return

    msg = EmailMessage()
    msg["Subject"] = "Daily UX Research Job Digest"
    msg["From"] = email_cfg.get("from_address")
    msg["To"] = email_cfg.get("to_address")
    msg.set_content(summary)

    smtp_server = email_cfg.get("smtp_server")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    username = email_cfg.get("username")
    password = (email_cfg.get("password") or "").strip().replace(" ", "")

    print(f"[INFO] Attempting to send email via {smtp_server}:{smtp_port} from {email_cfg.get('from_address')} to {email_cfg.get('to_address')} (user: {username})")

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.set_debuglevel(1)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)
            server.send_message(msg)
        print("[INFO] Daily email sent successfully.")
    except Exception as exc:
        print(f"[ERROR] Failed to send email: {type(exc).__name__}: {exc}")


def build_digest(results: List[Dict[str, Any]]) -> str:
    lines = ["Daily UX Research Job Digest", "===========================", ""]
    if not results:
        lines.append("No strong or moderate matches were found.")
        return "\n".join(lines)

    for idx, item in enumerate(results[:10], start=1):
        lines.append(f"{idx}. {item.get('title')} | {item.get('company')} | {item.get('location')} | {item.get('fit_tier')} | {item.get('match_score')}/100")
        lines.append(f"   URL: {item.get('url')}")
        lines.append(f"   Why it matches: {item.get('match_reasons', '')}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    ensure_output_dir()

    config = load_json(CONFIG_PATH)
    email_cfg = config.setdefault("email", {})
    if os.environ.get("EMAIL_ENABLED", "").lower() in {"1", "true", "yes"}:
        email_cfg["enabled"] = True
    for key, env_name in {
        "smtp_server": "SMTP_SERVER",
        "smtp_port": "SMTP_PORT",
        "username": "SMTP_USERNAME",
        "password": "SMTP_PASSWORD",
        "from_address": "EMAIL_FROM",
        "to_address": "EMAIL_TO",
    }.items():
        env_value = os.environ.get(env_name)
        if env_value:
            email_cfg[key] = env_value
    profile = load_profile(PROFILE_PATH)

    all_jobs = fetch_jobs_from_config(config)
    all_jobs = dedupe_jobs(all_jobs)

    scored_jobs = []
    for job in all_jobs:
        title = job.get("title") or ""
        if not is_target_role(title, config):
            continue
        result = score_job(job, profile)
        result["url"] = job.get("url") or ""
        result["match_reasons"] = (
            f"role match: {result['category_scores'].get('role_alignment', 0)}; "
            f"seniority match: {result['category_scores'].get('seniority_fit', 0)}; "
            f"method match: {result['category_scores'].get('methods_and_skills', 0)}"
        )
        scored_jobs.append(result)

    scored_jobs = sorted(scored_jobs, key=lambda item: item.get("match_score", 0), reverse=True)
    top_results = [job for job in scored_jobs if job.get("match_score", 0) >= 55][:20]

    save_json(RESULTS_PATH, top_results)
    save_top_matches_csv(top_results, TOP_MATCHES_PATH)

    previous_jobs = load_previous_jobs().get("jobs", [])
    previous_urls = {job.get("url") for job in previous_jobs}
    new_jobs = [job for job in top_results if job.get("url") and job.get("url") not in previous_urls]
    save_json(NEW_JOBS_PATH, new_jobs)

    save_json(PREVIOUS_RESULTS_PATH, {"jobs": top_results})

    digest = build_digest(new_jobs or top_results)
    if config.get("email", {}).get("enabled"):
        send_email_summary(digest, config)
    print(digest)

    print(f"[INFO] Found {len(top_results)} relevant jobs. {len(new_jobs)} are new since the last run.")


if __name__ == "__main__":
    main()
