"""Persistent record of every job the agent has handled, committed back to the repo."""
import json
import time
from datetime import datetime, timezone

from .config import ROOT
from .models import Job

STATE_FILE = ROOT / "state" / "jobs.json"
QUEUE_FILE = ROOT / "state" / "review_queue.md"
CONTROL_FILE = ROOT / "state" / "control.json"   # written by the tracker app

# Statuses that count against the daily cap.
SUBMITTED = "submitted"
DRY_RUN = "dry_run"
NEEDS_REVIEW = "needs_review"   # supported ATS, but the agent wasn't confident
MANUAL = "manual"               # ATS the agent can't drive (Workday, iCIMS, LinkedIn...)
FAILED = "failed"
SKIPPED = "skipped"             # filtered out; remembered so it isn't re-checked


def paused() -> bool:
    """The tracker app's pause switch."""
    try:
        return bool(json.loads(CONTROL_FILE.read_text(encoding="utf-8")).get("paused"))
    except (FileNotFoundError, ValueError):
        return False


class State:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        if STATE_FILE.exists():
            self.jobs = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    def seen(self, job: Job, mode: str) -> bool:
        entry = self.jobs.get(job.key)
        if entry is None:
            return False
        # Dry-run fills are retried for real once you switch to auto mode.
        if entry["status"] == DRY_RUN and mode == "auto":
            return False
        # Waiting on you to click a Workday verification email: try again next run.
        return not (entry["status"] == NEEDS_REVIEW and entry["detail"].startswith("account verification pending"))

    def record(self, job: Job, status: str, detail: str = "", answers: list | None = None):
        self.jobs[job.key] = {
            "status": status,
            "company": job.company,
            "title": job.title,
            "url": job.url,
            "source": job.source,
            "at": time.time(),
            "detail": detail,
            **({"answers": answers} if answers else {}),
        }

    def submitted_today(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).timestamp()
        return sum(1 for j in self.jobs.values() if j["status"] == SUBMITTED and j["at"] >= start)

    def save(self):
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.jobs, indent=1, ensure_ascii=False), encoding="utf-8")
        self._write_queue()

    def _write_queue(self):
        rows = sorted(
            (j for j in self.jobs.values() if j["status"] in (NEEDS_REVIEW, MANUAL, FAILED)),
            key=lambda j: j["at"], reverse=True,
        )[:300]
        submitted = sorted((j for j in self.jobs.values() if j["status"] == SUBMITTED),
                           key=lambda j: j["at"], reverse=True)[:100]
        fmt = lambda j: datetime.fromtimestamp(j["at"]).strftime("%m-%d %H:%M")
        lines = ["# Review queue", "",
                 "Jobs the agent found but did not submit. Apply to these yourself.", "",
                 "| When | Status | Job | Why |", "|---|---|---|---|"]
        for j in rows:
            why = j["detail"].replace("|", "/").replace("\n", " ")[:160]
            lines.append(f"| {fmt(j)} | {j['status']} | [{j['company']} - {j['title']}]({j['url']}) | {why} |")
        lines += ["", "# Submitted", "", "| When | Job |", "|---|---|"]
        lines += [f"| {fmt(j)} | [{j['company']} - {j['title']}]({j['url']}) |" for j in submitted]
        QUEUE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
