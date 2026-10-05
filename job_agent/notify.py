"""Phone notifications through ntfy.sh (free app; subscribe to your NTFY_TOPIC)."""
import logging
import os

import requests

log = logging.getLogger(__name__)


def queue_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://github.com/{repo}/blob/main/state/review_queue.md" if repo else None


def send(title: str, body: str, click: str | None = None, priority: str = "default", tags: str = ""):
    log.info("notify: %s | %s", title, body.replace("\n", " / "))
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15)
    except requests.RequestException as e:
        log.warning("notification failed: %s", e)
