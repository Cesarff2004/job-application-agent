"""Public job-board APIs of the ATSes we can apply through."""
from datetime import datetime

import requests

from ..models import Job


def _ts(iso: str | None) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else 0.0


def greenhouse(slug: str) -> list[Job]:
    data = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=30).json()
    return [
        Job(
            company=j.get("company_name") or slug,
            title=j["title"],
            # Link the hosted board directly; absolute_url is often the company's own site.
            url=f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}",
            source=f"greenhouse:{slug}",
            locations=[(j.get("location") or {}).get("name", "")],
            posted_at=_ts(j.get("first_published") or j.get("updated_at")),
        )
        for j in data.get("jobs", [])
    ]


def lever(slug: str) -> list[Job]:
    data = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=30).json()
    return [
        Job(
            company=slug,
            title=j["text"],
            url=j["hostedUrl"],
            source=f"lever:{slug}",
            locations=[(j.get("categories") or {}).get("location", "")],
            posted_at=j.get("createdAt", 0) / 1000,
            description=j.get("descriptionPlain", ""),
        )
        for j in data
    ]


def ashby(slug: str) -> list[Job]:
    data = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=30).json()
    return [
        Job(
            company=slug,
            title=j["title"].strip(),
            url=j["jobUrl"],
            source=f"ashby:{slug}",
            locations=[j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])],
            posted_at=_ts(j.get("publishedAt")),
            description=j.get("descriptionPlain", ""),
        )
        for j in data.get("jobs", [])
        if j.get("isListed", True)
    ]


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
