import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Query params that identify a job; everything else (utm_*, ref, source) is noise.
_KEEP_PARAMS = {"gh_jid", "ashby_jid", "lever-via", "jobid", "id", "token", "for"}


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    query = {k: v for k, v in parse_qs(p.query).items() if k.lower() in _KEEP_PARAMS}
    path = re.sub(r"/(apply|application)/?$", "", p.path).rstrip("/")
    return urlunparse((p.scheme or "https", p.netloc.lower(), path, "", urlencode(query, doseq=True), ""))


@dataclass
class Job:
    company: str
    title: str
    url: str
    source: str
    locations: list[str] = field(default_factory=list)
    posted_at: float = 0.0          # unix seconds
    category: str = ""
    sponsorship: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        return hashlib.sha1(canonical_url(self.url).encode()).hexdigest()[:16]

    def label(self) -> str:
        return f"{self.company} - {self.title}"
