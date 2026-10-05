import logging
import os

from . import ats, notify
from .filters import reject_reason
from .models import Job
from .sources import collect
from .state import DRY_RUN, FAILED, MANUAL, NEEDS_REVIEW, SUBMITTED, State, paused

log = logging.getLogger(__name__)


def new_jobs(settings: dict, state: State) -> tuple[list[Job], list[Job]]:
    """Unseen jobs that pass the filters, split into (automatable, manual)."""
    if paused():
        log.info("paused from the tracker app; skipping")
        return [], []
    mode = settings.get("mode", "dry_run")
    fresh = [j for j in collect(settings) if not state.seen(j, mode) and not reject_reason(j, settings)]
    auto = [j for j in fresh if ats.detect(j.url) in ats.SUPPORTED]
    manual = [j for j in fresh if ats.detect(j.url) not in ats.SUPPORTED]
    return auto, manual


def run(settings: dict):
    from playwright.sync_api import sync_playwright

    from . import browser
    from .answers import Answerer
    from .config import load_profile

    mode = settings.get("mode", "dry_run")
    state = State()
    auto, manual = new_jobs(settings, state)
    log.info("new matching jobs: %d automatable, %d manual (mode=%s)", len(auto), len(manual), mode)

    for job in manual:
        state.record(job, MANUAL, f"{ats.detect(job.url)} is not automated yet")
    if manual:
        lines = "\n".join(f"- {j.label()} ({ats.detect(j.url)})" for j in manual[:8])
        more = f"\n...and {len(manual) - 8} more" if len(manual) > 8 else ""
        notify.send(f"{len(manual)} new jobs to apply to yourself", lines + more, click=notify.queue_url(), tags="memo")

    budget = min(settings.get("max_applications_per_run", 5),
                 settings.get("max_applications_per_day", 25) - state.submitted_today())
    # GitHub Actions only installs the browser when the discover step found automatable jobs.
    # A job posted between that step and this one waits for the next run instead of crashing this one.
    if limit := os.environ.get("AUTO_LIMIT"):
        budget = min(budget, int(limit))
    todo = auto[:max(budget, 0)]
    if not todo:
        state.save()
        return

    profile = load_profile()
    answerer = Answerer(profile, settings)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        for job in todo:
            context = b.new_context(viewport={"width": 1280, "height": 1600}, locale="en-US")
            page = context.new_page()
            page.set_default_timeout(15000)
            try:
                result = browser.apply(page, job, profile, answerer, settings, submit=mode == "auto")
            except Exception as e:
                log.exception("apply failed for %s", job.label())
                result = browser.Result(FAILED, f"{type(e).__name__}: {e}"[:300])
            finally:
                context.close()
            log.info("%s -> %s %s", job.label(), result.status, result.detail)
            state.record(job, result.status, result.detail, result.answers)
            state.save()  # save after every job so a crash never causes a double-apply
            _announce(job, result)
        b.close()
    state.save()


def _announce(job: Job, result):
    if result.status == SUBMITTED:
        notify.send(f"Applied: {job.company}", job.title, click=job.url, tags="white_check_mark")
    elif result.status == NEEDS_REVIEW:
        notify.send(f"Needs you: {job.company}", f"{job.title}\n{result.detail}", click=job.url,
                    priority="high", tags="warning")
    elif result.status == FAILED:
        notify.send(f"Failed: {job.company}", f"{job.title}\n{result.detail}\nCheck whether it went through.",
                    click=job.url, priority="high", tags="x")
    elif result.status == DRY_RUN:
        notify.send(f"Dry run filled: {job.company}", job.title, click=job.url, priority="low", tags="test_tube")
