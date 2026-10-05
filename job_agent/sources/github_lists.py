"""Job lists maintained in GitHub repos."""
import re
import time

import requests

from ..models import Job

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def _get(repo: str, path: str, branches=("dev", "main", "master")) -> requests.Response:
    for branch in branches:
        r = requests.get(RAW.format(repo=repo, branch=branch, path=path), timeout=60)
        if r.ok:
            return r
    r.raise_for_status()


def fetch_simplify(repo: str) -> list[Job]:
    """SimplifyJobs repos keep structured data in .github/scripts/listings.json."""
    listings = _get(repo, ".github/scripts/listings.json").json()
    return [
        Job(
            company=x["company_name"],
            title=x["title"].strip(),
            url=x["url"],
            source=repo,
            locations=x.get("locations", []),
            posted_at=x.get("date_posted", 0),
            category=x.get("category", ""),
            sponsorship=x.get("sponsorship", ""),
        )
        for x in listings
        if x.get("active") and x.get("is_visible", True)
    ]


_LINK = re.compile(r'(?:href="|\]\()(https?://[^")\s]+)')
_TAGS = re.compile(r"<[^>]+>|\*\*|\[|\]\([^)]*\)")
_SKIP_HOSTS = ("simplify.jobs", "imgur.com", "i.imgur", "github.com", "shields.io")


def _cell_text(cell: str) -> str:
    return _TAGS.sub("", cell).strip()


def fetch_markdown(repo: str) -> list[Job]:
    """Generic parser for README tables shaped like | Company | Role | Location | Apply | Date |.

    Rows starting with ↳ inherit the company from the row above. Postings have no
    reliable timestamp, so they get "now"; the seen-state stops repeats.
    """
    text = _get(repo, "README.md", branches=("main", "master", "dev")).text
    jobs, company, now = [], "", time.time()
    for line in text.splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() in ("company", "name"):
            continue
        if "🔒" in line:  # closed postings
            continue
        links = [u for u in _LINK.findall(line) if not any(h in u for h in _SKIP_HOSTS)]
        if not links:
            continue
        name = _cell_text(cells[0])
        company = company if name in ("↳", "") else name
        jobs.append(Job(
            company=company,
            title=_cell_text(cells[1]),
            url=links[-1],  # apply link is usually the last column with a link
            source=repo,
            locations=[_cell_text(cells[2])],
            posted_at=now,
        ))
    return jobs
