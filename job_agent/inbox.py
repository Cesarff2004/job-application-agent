"""Reads replies from employers in your inbox and moves applications along the tracker stages.

Every run it looks at new emails, keeps the ones that mention a company you applied to,
and asks Claude what each one means (rejection, assessment, interview, offer). Results go
to state/inbox.json, which only the agent writes; the tracker app merges them with your
own updates and shows whichever happened last.
"""
import email
import email.utils
import json
import logging
import re
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header

import anthropic

from . import notify
from .config import ROOT
from .sources.email_alerts import _connect, _html_body
from .state import STATE_FILE, SUBMITTED

log = logging.getLogger(__name__)
INBOX_FILE = ROOT / "state" / "inbox.json"
TRACKING_FILE = ROOT / "state" / "tracking.json"

STAGE_NAMES = {"rejected": "Rejected", "oa": "Assessment", "interview": "Interview", "offer": "Offer"}
STAGES = set(STAGE_NAMES)  # tracker stage ids an email can move a job to
_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|technologies|technology|"
                     r"labs|systems|the|plc|lp|na)\b\.?", re.I)
_NOISE_SENDERS = re.compile(r"linkedin|indeed|glassdoor|handshake|simplify|ziprecruiter|newsletter|marketing", re.I)

SYSTEM = """You read emails a job applicant received and decide what each says about an application.

For each email return:
- application: the key of the application it is about, copied from that email's candidates, or "" if none fits
  (job alerts, newsletters, marketing and unrelated mail are "").
- stage: one of
  "received"  - confirmation that the application was received / is under review
  "rejected"  - not moving forward, position filled, other candidates selected
  "oa"        - an online assessment, coding challenge, HackerRank/CodeSignal or take-home invitation
  "interview" - an invitation to schedule or attend an interview or recruiter call, or a confirmed interview time
  "offer"     - a job offer
  "other"     - anything else
- confidence: 0 to 1.
- summary: under 15 words, plain."""

SCHEMA = {
    "type": "object",
    "properties": {"emails": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "application": {"type": "string"},
                       "stage": {"type": "string", "enum": ["received", "rejected", "oa", "interview", "offer", "other"]},
                       "confidence": {"type": "number"}, "summary": {"type": "string"}},
        "required": ["id", "application", "stage", "confidence", "summary"],
        "additionalProperties": False}}},
    "required": ["emails"],
    "additionalProperties": False,
}


def _norm(company: str) -> str:
    return re.sub(r"[^a-z0-9& ]+", " ", _SUFFIX.sub(" ", company.lower())).strip()


def _header(msg, name: str) -> str:
    return str(make_header(decode_header(msg.get(name, ""))))


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def applications() -> dict[str, dict]:
    """Everything applied to: agent submissions plus jobs you marked applied in the tracker."""
    jobs, tracking = _load(STATE_FILE, {}), _load(TRACKING_FILE, {})
    apps = {k: {"company": j["company"], "title": j["title"]} for k, j in jobs.items() if j["status"] == SUBMITTED}
    for k, t in tracking.items():
        if t.get("stage") and t.get("stage") not in ("withdrawn",):
            src = jobs.get(k) or t
            if src.get("company"):
                apps[k] = {"company": src["company"], "title": src.get("title", "")}
    return apps


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9& ]+", " ", text.lower()) + " "


def _candidates(text: str, apps: dict[str, dict], sender: str = "") -> list[dict]:
    hay, frm = _words(text), _words(sender)
    out = []
    for key, a in apps.items():
        name = _norm(a["company"])
        # Short names ("Q2", "EA") only count in the sender, where they can't be an ordinary word.
        if name and f" {name} " in (hay if len(name) >= 3 else frm):
            out.append({"key": key, "company": a["company"], "title": a["title"]})
    return out[:6]


def check(settings: dict, lookback_days: int = 3) -> int:
    """Classify new employer emails. Returns how many applications changed stage."""
    apps = applications()
    if not apps:
        return 0
    data = _load(INBOX_FILE, {"updates": {}, "seen": []})
    seen = set(data["seen"])

    box = _connect()
    batch = []
    try:
        since = datetime.now() - timedelta(days=lookback_days)
        _, ids = box.search(None, f'(SINCE {since.strftime("%d-%b-%Y")})')
        for i in ids[0].split()[-300:]:
            _, head = box.fetch(i, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
            h = email.message_from_bytes(head[0][1])
            mid = h.get("Message-ID") or f"{h.get('Date')}|{h.get('Subject')}"
            if mid in seen:
                continue
            seen.add(mid)
            sender, subject = _header(h, "From"), _header(h, "Subject")
            if _NOISE_SENDERS.search(sender):
                continue
            _, full = box.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1])
            body = _html_body(msg)
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        break
            body = re.sub(r"\s+", " ", re.sub(r"<style.*?</style>|<[^>]+>", " ", body, flags=re.S))[:3000]
            cands = _candidates(f"{sender} {subject} {body}", apps, sender)
            if not cands:
                continue
            try:
                at = email.utils.parsedate_to_datetime(h["Date"]).timestamp()
            except (TypeError, ValueError):
                at = time.time()
            batch.append({"id": str(len(batch)), "mid": mid, "at": at, "from": sender, "subject": subject,
                          "body": body, "candidates": cands})
    finally:
        box.logout()

    changed = 0
    for start in range(0, len(batch), 15):
        chunk = batch[start:start + 15]
        result = _classify(chunk, data["updates"], settings)
        if result is None:  # not classified: leave these unseen so the next run tries again
            seen.difference_update(e["mid"] for e in chunk)
        else:
            changed += result
    data["seen"] = list(seen)[-3000:]
    INBOX_FILE.parent.mkdir(exist_ok=True)
    INBOX_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("inbox: %d employer emails, %d stage changes", len(batch), changed)
    return changed


def _classify(batch: list[dict], updates: dict, settings: dict) -> int | None:
    """Returns how many applications changed stage, or None if Claude gave no usable answer."""
    if not batch:
        return 0
    cfg = settings.get("claude", {})
    shown = [{k: e[k] for k in ("id", "from", "subject", "body", "candidates")} for e in batch]
    response = anthropic.Anthropic().messages.create(
        model=cfg.get("model", "claude-opus-5-5"),
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(shown, ensure_ascii=False)}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if response.stop_reason != "end_turn":
        log.warning("inbox classification stopped with %s", response.stop_reason)
        return None
    text = next(b.text for b in response.content if b.type == "text")
    by_id = {e["id"]: e for e in batch}
    changed = 0
    for r in json.loads(text)["emails"]:
        e = by_id.get(r["id"])
        valid = e and r["application"] in {c["key"] for c in e["candidates"]}
        if not valid or r["stage"] not in STAGES or r["confidence"] < 0.7:
            continue
        entry = updates.setdefault(r["application"], {"events": []})
        if any(ev.get("mid") == e["mid"] for ev in entry["events"]):
            continue
        entry["events"].append({"stage": r["stage"], "at": e["at"], "subject": e["subject"][:200],
                                "summary": r["summary"], "mid": e["mid"]})
        entry["events"].sort(key=lambda ev: ev["at"])
        entry["stage"] = entry["events"][-1]["stage"]
        changed += 1
        company = next(c["company"] for c in e["candidates"] if c["key"] == r["application"])
        log.info("inbox: %s -> %s (%s)", company, r["stage"], r["summary"])
        if r["stage"] in ("oa", "interview", "offer"):
            notify.send(f"{company}: {STAGE_NAMES[r['stage']]}", f"{r['summary']}\n{e['subject']}",
                        priority="high", tags="tada" if r["stage"] == "offer" else "calendar")
    return changed
