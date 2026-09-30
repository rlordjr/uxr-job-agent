from datetime import datetime, timedelta, timezone
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
# Committed to the repo by the workflow so "already shown" survives between Actions runs
# (output/ is gitignored and starts empty on every runner).
SEEN_JOBS_PATH = os.path.join(os.path.dirname(__file__), "data", "seen_jobs.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "data", "history.json")
SEEN_RETENTION_DAYS = 90
MAX_STILL_OPEN_SHOWN = 20
RESULTS_PATH = os.path.join(OUTPUT_DIR, "results.json")
NEW_JOBS_PATH = os.path.join(OUTPUT_DIR, "new_jobs.json")
TOP_MATCHES_PATH = os.path.join(OUTPUT_DIR, "top_matches.csv")


def ensure_output_dir() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_dedupe_key(value: str) -> str:
    return (value or "").strip().lower()


def dedupe_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe by title+company, ignoring location, since aggregators like Adzuna often
    index the same posting once per nearby city/suburb. Keeps the most recently posted
    variant among duplicates (falls back to first-seen if dates are missing/unparseable).
    """
    best_by_key: Dict[tuple, Dict[str, Any]] = {}
    for job in jobs:
        key = (_normalize_dedupe_key(job.get("title")), _normalize_dedupe_key(job.get("company")))
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = job
            continue
        if _posted_at_sort_key(job) > _posted_at_sort_key(existing):
            best_by_key[key] = job
    return list(best_by_key.values())


def _posted_at_sort_key(job: Dict[str, Any]) -> str:
    return str(job.get("posted_at") or "")


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


def passes_company_location_allowlist(job: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """Some companies (e.g. Cox Automotive via Adzuna) post the same role repeatedly across
    many nearby suburbs, flooding results. If a company has an entry in
    company_location_allowlist, only keep postings whose location matches one of the
    allowed keywords (e.g. specific counties); other companies are unaffected.
    """
    allowlist = config.get("company_location_allowlist", {})
    if not allowlist:
        return True

    company = (job.get("company") or "").strip().lower()
    allowed_keywords = allowlist.get(company)
    if not allowed_keywords:
        return True

    location = (job.get("location") or "").strip().lower()
    return any(keyword.lower() in location for keyword in allowed_keywords)


def save_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def save_top_matches_csv(results: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("new,first_seen,title,company,location,posted_at,fit_tier,match_score,url\n")
        for item in results:
            posted = format_posted_date(item.get("posted_at"))
            f.write(
                f"{'yes' if item.get('is_new') else 'no'},{item.get('first_seen','')},\"{item.get('title','')}\",\"{item.get('company','')}\",\"{item.get('location','')}\",\"{posted}\",\"{item.get('fit_tier','')}\",{item.get('match_score','')},\"{item.get('url','')}\"\n"
            )


def _url_key(job: Dict[str, Any]) -> str:
    return _normalize_dedupe_key(job.get("url"))


def _title_company_key(job: Dict[str, Any]) -> str:
    return f"{_normalize_dedupe_key(job.get('title'))}|{_normalize_dedupe_key(job.get('company'))}"


class SeenJobs:
    """Jobs already shown in a previous run, matched by URL or by title+company
    (Adzuna redirect URLs can change between runs for the same posting)."""

    def __init__(self, entries: List[Dict[str, Any]]):
        self.entries = entries
        self._by_url = {}
        self._by_title_company = {}
        for entry in entries:
            self._index(entry)

    def _index(self, entry: Dict[str, Any]) -> None:
        if _url_key(entry):
            self._by_url[_url_key(entry)] = entry
        self._by_title_company[_title_company_key(entry)] = entry

    def find(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return self._by_url.get(_url_key(job)) or self._by_title_company.get(_title_company_key(job))

    def mark_seen(self, job: Dict[str, Any], today: str) -> None:
        entry = self.find(job)
        if entry is None:
            entry = {"title": job.get("title"), "company": job.get("company"), "url": job.get("url"), "first_seen": today}
            self.entries.append(entry)
        entry["last_seen"] = today
        self._index(entry)

    def prune(self, retention_days: int) -> None:
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        self.entries = [e for e in self.entries if e.get("last_seen", "") >= cutoff]


def _seed_seen_entries_from_history() -> List[Dict[str, Any]]:
    """First run with the seen-jobs file: treat everything already on the dashboard as seen."""
    if not os.path.exists(HISTORY_PATH):
        return []
    seen = SeenJobs([])
    for run in sorted(load_json(HISTORY_PATH).get("runs", []), key=lambda r: r.get("run_date", "")):
        for job in run.get("jobs", []):
            seen.mark_seen(job, run.get("run_date", ""))
    return seen.entries


def load_seen_jobs() -> SeenJobs:
    if os.path.exists(SEEN_JOBS_PATH):
        return SeenJobs(load_json(SEEN_JOBS_PATH).get("jobs", []))
    return SeenJobs(_seed_seen_entries_from_history())


def save_seen_jobs(seen: SeenJobs) -> None:
    os.makedirs(os.path.dirname(SEEN_JOBS_PATH), exist_ok=True)
    save_json(SEEN_JOBS_PATH, {"jobs": seen.entries})


def parse_posted_date(raw_date: Any):
    """Returns a UTC-aware datetime, or None if the date is missing/unparseable."""
    raw_str = str(raw_date or "").strip()
    if not raw_str:
        return None
    try:
        if raw_str.isdigit():
            ts = int(raw_str)
            if ts > 1e11:  # milliseconds
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def is_within_window(job: Dict[str, Any], window_days: int) -> bool:
    posted = parse_posted_date(job.get("posted_at"))
    if posted is None:
        return True  # Can't tell how old it is; let it through rather than silently drop it.
    return posted >= datetime.now(timezone.utc) - timedelta(days=window_days)


def send_email_summary(summary: str, config: Dict[str, Any], attachment_path: str = None, subject: str = "Daily UX Research Job Digest") -> None:
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
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(summary)

    if attachment_path and os.path.exists(attachment_path):
        try:
            with open(attachment_path, "rb") as f:
                csv_bytes = f.read()
            msg.add_attachment(
                csv_bytes,
                maintype="text",
                subtype="csv",
                filename=os.path.basename(attachment_path),
            )
            print(f"[INFO] Attached {attachment_path} to email.")
        except Exception as exc:
            print(f"[WARN] Failed to attach {attachment_path}: {exc}")
    elif attachment_path:
        print(f"[WARN] Attachment path not found, skipping attachment: {attachment_path}")

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
    raw_str = str(raw_date or "").strip()
    if not raw_str:
        return "Not specified"
    dt = parse_posted_date(raw_str)
    if dt:
        return dt.strftime("%Y-%m-%d")
    # Return first 10 characters if YYYY-MM-DD
    if len(raw_str) >= 10 and raw_str[:10].count("-") == 2:
        return raw_str[:10]
    return raw_str


def build_digest(new_jobs: List[Dict[str, Any]], still_open: List[Dict[str, Any]]) -> str:
    lines = [
        "Daily UX Research Job Digest",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M EST')}",
        "==================================================",
        ""
    ]
    if new_jobs:
        lines.append(f"NEW SINCE LAST RUN ({len(new_jobs)})")
        lines.append("")
    else:
        lines.append("No new matches today.")
        lines.append("")

    for idx, item in enumerate(new_jobs, start=1):
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

    if still_open:
        lines.append(f"STILL OPEN - ALREADY SENT ({len(still_open)})")
        for item in still_open:
            lines.append(f"- {item.get('title')} | {item.get('company')} | {item.get('match_score')}/100 | first seen {item.get('first_seen')} | {item.get('url')}")

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

    window_days = int(config.get("search_window_days", 30))
    fetched_count = len(all_jobs)
    all_jobs = [job for job in all_jobs if is_within_window(job, window_days)]
    print(f"[INFO] Dropped {fetched_count - len(all_jobs)} job(s) posted more than {window_days} days ago.")

    # Region priority order for sorting: US first, then Canada/Caribbean, then Mexico/South America.
    REGION_PRIORITY = {"united_states": 0, "canada_caribbean": 1, "mexico_south_america": 2}

    scored_jobs = []
    for job in all_jobs:
        title = job.get("title") or ""
        if not is_target_role(title, config):
            continue
        if not passes_company_location_allowlist(job, config):
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
    qualifying = [job for job in scored_jobs if job.get("match_score", 0) >= 55]

    today = datetime.now().strftime("%Y-%m-%d")
    seen = load_seen_jobs()
    new_jobs, still_open = [], []
    for job in qualifying:
        entry = seen.find(job)
        job["is_new"] = entry is None
        job["first_seen"] = entry.get("first_seen") if entry else today
        (still_open if entry else new_jobs).append(job)

    # Every new job is shown; already-sent jobs only fill in behind them.
    still_open = still_open[:MAX_STILL_OPEN_SHOWN]
    top_results = new_jobs + still_open

    save_json(RESULTS_PATH, top_results)
    save_top_matches_csv(top_results, TOP_MATCHES_PATH)
    save_json(NEW_JOBS_PATH, new_jobs)

    for job in qualifying:
        seen.mark_seen(job, today)
    seen.prune(SEEN_RETENTION_DAYS)
    save_seen_jobs(seen)

    digest = build_digest(new_jobs, still_open)
    subject = f"Daily UX Research Job Digest - {len(new_jobs)} new" if new_jobs else "Daily UX Research Job Digest - no new matches"
    if config.get("email", {}).get("enabled"):
        send_email_summary(digest, config, attachment_path=TOP_MATCHES_PATH, subject=subject)
    print(digest)

    print(f"[INFO] Found {len(qualifying)} relevant jobs. {len(new_jobs)} are new since the last run.")


if __name__ == "__main__":
    main()
