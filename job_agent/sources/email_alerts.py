"""LinkedIn / Indeed job-alert emails, read from your own inbox over IMAP.

The agent never logs into LinkedIn or Indeed (their terms forbid automation and
accounts get banned). It reads the alert emails those sites send you, then tries
to find the company's own application page so it can apply there instead.
"""
import email
import email.utils
import html
import imaplib
import os
import re
import time
from datetime import datetime, timedelta
from email.message import Message
from urllib.parse import parse_qs, unquote, urlparse

import requests

from ..models import Job

SENDERS = ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com", "alert@indeed.com", "noreply@indeed.com"]
_ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_LI_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_IN_JOB = re.compile(r"indeed\.com/.*?[?&]jk=([0-9a-f]+)")


def _connect() -> imaplib.IMAP4_SSL:
    user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not user or not password:
        raise RuntimeError("IMAP_USER / IMAP_PASSWORD not set")
    box = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
    box.login(user, password)
    box.select("INBOX", readonly=True)
    return box


def _html_body(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _search(box, sender: str, since: datetime) -> list[Message]:
    _, ids = box.search(None, f'(FROM "{sender}" SINCE {since.strftime("%d-%b-%Y")})')
    msgs = []
    for i in ids[0].split():
        _, data = box.fetch(i, "(RFC822)")
        msgs.append(email.message_from_bytes(data[0][1]))
    return msgs


def resolve_linkedin(job_id: str) -> str | None:
    """Public (logged-out) job page embeds the external apply URL for non-Easy-Apply jobs."""
    r = requests.get(f"https://www.linkedin.com/jobs/view/{job_id}",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    m = re.search(r'<code id="applyUrl"[^>]*><!--"(.+?)"-->', r.text)
    if not m:
        return None
    url = html.unescape(m.group(1))
    target = parse_qs(urlparse(url).query).get("url")
    return unquote(target[0]) if target else url


def fetch(lookback_days: int = 2) -> list[Job]:
    box = _connect()
    since = datetime.now() - timedelta(days=lookback_days)
    jobs: dict[str, Job] = {}
    try:
        for sender in SENDERS:
            for msg in _search(box, sender, since):
                posted = email.utils.parsedate_to_datetime(msg["Date"]).timestamp()
                for href, text in _ANCHOR.findall(_html_body(msg)):
                    href = html.unescape(href)
                    title = re.sub(r"<[^>]+>|\s+", " ", text).strip()
                    if len(title) < 4:
                        continue
                    if m := _LI_JOB.search(href):
                        url, site = f"https://www.linkedin.com/jobs/view/{m.group(1)}", "linkedin"
                    elif m := _IN_JOB.search(href):
                        url, site = f"https://www.indeed.com/viewjob?jk={m.group(1)}", "indeed"
                    else:
                        continue
                    jobs.setdefault(url, Job(company=f"({site} alert)", title=title, url=url,
                                             source=f"email:{site}", posted_at=posted))
    finally:
        box.logout()

    for job in jobs.values():
        if job.source == "email:linkedin":
            try:
                external = resolve_linkedin(job.url.rsplit("/", 1)[-1])
            except requests.RequestException:
                external = None
            if external:
                job.url = external
            time.sleep(1)
    return list(jobs.values())


def _text_lines(msg: Message) -> list[str]:
    """The email's visible text, one block element per line."""
    body = _html_body(msg)
    if not body:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                break
    body = re.sub(r"<style.*?</style>|<script.*?</script>", " ", body, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    return [l.strip() for l in text.splitlines() if l.strip()]


_CODE = re.compile(r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,8}")  # 6-8 characters with at least one digit


def extract_code(msg: Message) -> str | None:
    """The one-time code in a verification email. Greenhouse puts it on a line of its own
    after "...paste this code into the security code field on your application:"."""
    lines = _text_lines(msg)
    start = next((i for i, l in enumerate(lines) if re.search(r"\bcode\b", l, re.I)), None)
    if start is None:
        return None
    for line in lines[start:start + 8]:
        if _CODE.fullmatch(line):
            return line
    # Code inline at the end of the sentence: "Your security code is: AB12CD34"
    for line in lines[start:start + 8]:
        if m := re.search(r"\bcode\b[^:]*:\s*([A-Za-z0-9]{6,8})\b", line, re.I):
            if _CODE.fullmatch(m.group(1)):
                return m.group(1)
    return None


def wait_for_code(after: float, timeout: int = 120) -> str | None:
    """Poll the inbox for a verification code some ATSes (Greenhouse) email after submit."""
    if not os.environ.get("IMAP_USER"):
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        box = _connect()
        try:
            _, ids = box.search(None, f'(SINCE {datetime.now().strftime("%d-%b-%Y")} SUBJECT "code")')
            for i in reversed(ids[0].split()[-10:]):
                _, data = box.fetch(i, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                if email.utils.parsedate_to_datetime(msg["Date"]).timestamp() < after - 60:
                    continue
                if code := extract_code(msg):
                    return code
        finally:
            box.logout()
        time.sleep(10)
    return None


def wait_for_link(after: float, pattern: str, timeout: int = 180) -> str | None:
    """Poll the inbox for a verification link (Workday account sign-up) and return it."""
    if not os.environ.get("IMAP_USER"):
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        box = _connect()
        try:
            _, ids = box.search(None, f'(SINCE {datetime.now().strftime("%d-%b-%Y")})')
            for i in reversed(ids[0].split()[-15:]):
                _, data = box.fetch(i, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                if email.utils.parsedate_to_datetime(msg["Date"]).timestamp() < after - 60:
                    continue
                if not re.search(r"verif|activate|confirm", msg.get("Subject", ""), re.I):
                    continue
                body = html.unescape(_html_body(msg) or str(msg.get_payload()))
                if m := re.search(pattern, body):
                    return m.group(0)
        finally:
            box.logout()
        time.sleep(10)
    return None
