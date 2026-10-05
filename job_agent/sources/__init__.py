import logging

from ..models import Job
from . import ats_boards, email_alerts, github_lists

log = logging.getLogger(__name__)


def collect(settings: dict) -> list[Job]:
    """Pull jobs from every configured source, deduplicated, newest first."""
    src = settings.get("sources", {})
    fetchers = [
        *[(f"simplify:{r}", lambda r=r: github_lists.fetch_simplify(r)) for r in src.get("simplify", [])],
        *[(f"markdown:{r}", lambda r=r: github_lists.fetch_markdown(r)) for r in src.get("github_markdown", [])],
        *[(f"{ats}:{slug}", lambda a=ats, s=slug: ats_boards.FETCHERS[a](s))
          for ats, slugs in (src.get("ats_boards") or {}).items() for slug in slugs or []],
    ]
    alerts = src.get("email_alerts") or {}
    if alerts.get("enabled"):
        fetchers.append(("email_alerts", lambda: email_alerts.fetch(alerts.get("lookback_days", 2))))

    jobs: dict[str, Job] = {}
    for name, fetch in fetchers:
        try:
            found = fetch()
        except Exception as e:  # one broken source shouldn't stop the run
            log.warning("source %s failed: %s", name, e)
            continue
        log.info("source %s: %d jobs", name, len(found))
        for job in found:
            jobs.setdefault(job.key, job)
    return sorted(jobs.values(), key=lambda j: j.posted_at, reverse=True)
