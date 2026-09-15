from datetime import datetime
import json
import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List

from job_sources import fetch_jobs_from_config
from scorer import ALLOWED_REGIONS, load_profile, score_job

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

    # Hard exclude check - if any exclude keyword is in title, reject immediately
    for excl in exclude_keywords:
        if excl.lower() in title:
            return False

    # Check for direct target roles match
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
        f.write("title,company,location,posted_at,fit_tier,match_score,url\n")
        for item in results:
            posted = format_posted_date(item.get("posted_at"))
            f.write(
                f"\"{item.get('title','')}\",\"{item.get('company','')}\",\"{item.get('location','')}\",\"{posted}\",\"{item.get('fit_tier','')}\",{item.get('match_score','')},\"{item.get('url','')}\"\n"
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

    from_addr = email_cfg.get("from_address")
    to_addr = email_cfg.get("to_address")
    smtp_server = email_cfg.get("smtp_server")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    username = email_cfg.get("username")
    password = (email_cfg.get("password") or "").strip().replace(" ", "")

    print(f"[INFO] Email config summary: server={smtp_server}, port={smtp_port}, user={username}, from={from_addr}, to={to_addr}")

    if not to_addr or not from_addr or not username or not password:
        print(f"[ERROR] Missing required email fields! from={bool(from_addr)}, to={bool(to_addr)}, user={bool(username)}, pass={bool(password)}")
        return

    msg = EmailMessage()
    msg["Subject"] = "Daily UX Research Job Digest"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(summary)

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.set_debuglevel(1)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)
            send_errs = server.send_message(msg)
            if send_errs:
                print(f"[WARN] send_message returned recipient errors: {send_errs}")
            else:
                print("[INFO] Daily email sent successfully (server accepted message).")
    except Exception as exc:
        print(f"[ERROR] Failed to send email: {type(exc).__name__}: {exc}")


def format_posted_date(raw_date: Any) -> str:
    if not raw_date:
        return "Not specified"
    raw_str = str(raw_date).strip()
    if not raw_str:
        return "Not specified"
    # Try timestamp in ms
    if raw_str.isdigit():
        try:
            ts = int(raw_str)
            if ts > 1e11:  # milliseconds
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            pass
    # Try ISO string
    try:
        clean_iso = raw_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        # Return first 10 characters if YYYY-MM-DD
        if len(raw_str) >= 10 and raw_str[:10].count("-") == 2:
            return raw_str[:10]
        return raw_str


def build_digest(results: List[Dict[str, Any]]) -> str:
    lines = [
        "Daily UX Research Job Digest",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M EST')}",
        "==================================================",
        ""
    ]
    if not results:
        lines.append("No strong or moderate matches were found.")
        return "\n".join(lines)

    for idx, item in enumerate(results[:10], start=1):
        posted_str = format_posted_date(item.get("posted_at"))
        lines.append(f"{idx}. {item.get('title')} | {item.get('company')}")
        lines.append(f"   • Location: {item.get('location')}")
        lines.append(f"   • Date Posted: {posted_str}")
        lines.append(f"   • Fit Score: {item.get('match_score')}/100 [{item.get('fit_tier')}]")
        lines.append(f"   • Direct Link: {item.get('url')}")
        
        signals = item.get("signals", {})
        if signals:
            lines.append("   • Fit Breakdown:")
            lines.append(f"     - Role: {signals.get('role', 'N/A')}")
            lines.append(f"     - Seniority: {signals.get('seniority', 'N/A')}")
            lines.append(f"     - Qualitative Methods: {signals.get('methods', 'N/A')}")
            lines.append(f"     - Leadership / Influence: {signals.get('leadership', 'N/A')}")
        elif item.get("match_reasons"):
            lines.append(f"   • Rationale: {item.get('match_reasons')}")
            
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    ensure_output_dir()

    config = load_json(CONFIG_PATH)
    email_cfg = config.setdefault("email", {})
    
    # Track which settings were detected
    sources_found = []

    if os.environ.get("EMAIL_ENABLED", "").lower() in {"1", "true", "yes"}:
        email_cfg["enabled"] = True
        sources_found.append("EMAIL_ENABLED=true")

    # 1. Check if user provided all email configuration as one secret
    combo_secret = os.environ.get("EMAIL_JOB_RESULTS", "")
    if combo_secret:
        email_cfg["enabled"] = True
        sources_found.append("EMAIL_JOB_RESULTS secret found")
        try:
            parsed = json.loads(combo_secret)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    email_cfg[k.lower()] = v
        except Exception:
            for line in combo_secret.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                elif ":" in line:
                    k, v = line.split(":", 1)
                else:
                    continue
                k_clean = k.strip().lower().replace("smtp_", "").replace("email_", "")
                v_clean = v.strip().strip("\"'")
                if k_clean in ["server", "host"]:
                    email_cfg["smtp_server"] = v_clean
                elif k_clean == "port":
                    email_cfg["smtp_port"] = v_clean
                elif k_clean in ["username", "user"]:
                    email_cfg["username"] = v_clean
                elif k_clean in ["password", "pass", "app_password"]:
                    email_cfg["password"] = v_clean
                elif k_clean in ["from", "from_address", "sender"]:
                    email_cfg["from_address"] = v_clean
                elif k_clean in ["to", "to_address", "recipient"]:
                    email_cfg["to_address"] = v_clean

    # 2. Check individual environment variables
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
            email_cfg["enabled"] = True
            sources_found.append(f"{env_name} found")

    print(f"[DEBUG] Email configuration status: enabled={email_cfg.get('enabled')}")
    print(f"[DEBUG] Detected sources: {sources_found if sources_found else 'None (all env variables were empty)'}")
    print(f"[DEBUG] Fields present -> server: {bool(email_cfg.get('smtp_server'))}, port: {email_cfg.get('smtp_port')}, user: {bool(email_cfg.get('username'))}, pass: {bool(email_cfg.get('password'))}, to: {bool(email_cfg.get('to_address'))}")

    profile = load_profile(PROFILE_PATH)

    all_jobs = fetch_jobs_from_config(config)
    all_jobs = dedupe_jobs(all_jobs)

    # Region priority order for sorting: US first, then Canada/Caribbean, then Mexico/South America.
    REGION_PRIORITY = {"united_states": 0, "canada_caribbean": 1, "mexico_south_america": 2}

    scored_jobs = []
    for job in all_jobs:
        title = job.get("title") or ""
        if not is_target_role(title, config):
            continue
        result = score_job(job, profile)
        if result.get("region") not in ALLOWED_REGIONS:
            continue  # Exclude Europe, Asia, and any other non-Americas/unclear locations
        result["url"] = job.get("url") or ""
        result["match_reasons"] = (
            f"role match: {result['category_scores'].get('role_alignment', 0)}; "
            f"seniority match: {result['category_scores'].get('seniority_fit', 0)}; "
            f"method match: {result['category_scores'].get('methods_and_skills', 0)}"
        )
        scored_jobs.append(result)

    scored_jobs = sorted(
        scored_jobs,
        key=lambda item: (REGION_PRIORITY.get(item.get("region"), 3), -item.get("match_score", 0)),
    )
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
