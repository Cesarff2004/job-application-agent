"""Applicant accounts the agent created or used on sites that require one (Workday).

One password, from the ATS_PASSWORD secret, is used for every account, so you can sign
in to any of them yourself. This file never stores it: state/accounts.json only records
which sites have an account and when it was last used.
"""
import json
import os
import time
from urllib.parse import urlparse

from .config import ROOT

ACCOUNTS_FILE = ROOT / "state" / "accounts.json"


def password() -> str:
    pw = os.environ.get("ATS_PASSWORD", "")
    if not pw:
        raise RuntimeError("ATS_PASSWORD is not set; it is needed to create or sign in to accounts")
    return pw


def site_key(url: str) -> str:
    """Workday accounts belong to the tenant (the host), shared by all of its career sites."""
    return urlparse(url).netloc.lower()


class Accounts:
    def __init__(self):
        self.sites: dict[str, dict] = {}
        if ACCOUNTS_FILE.exists():
            self.sites = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))

    def get(self, url: str) -> dict | None:
        return self.sites.get(site_key(url))

    def record(self, url: str, email: str, company: str = "", **extra):
        key = site_key(url)
        entry = self.sites.setdefault(key, {"email": email, "company": company, "created": time.time()})
        entry.update(extra, last_used=time.time())
        if company and not entry.get("company"):
            entry["company"] = company
        self.save()

    def save(self):
        ACCOUNTS_FILE.parent.mkdir(exist_ok=True)
        ACCOUNTS_FILE.write_text(json.dumps(self.sites, indent=1, sort_keys=True), encoding="utf-8")
