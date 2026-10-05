import re
import time

from .models import Job

# A location naming one of these is outside the US. Simplify writes "City, ST" for US
# postings and "City, Country" (or a province code) elsewhere.
_FOREIGN = re.compile(
    r"\b(canada|uk|united kingdom|england|scotland|ireland|northern ireland|india|china|japan|"
    r"singapore|germany|france|spain|italy|netherlands|poland|israel|mexico|brazil|australia|"
    r"korea|taiwan|hong kong|philippines|vietnam|romania|czech|switzerland|sweden|denmark|"
    r"belgium|portugal|austria|hungary|argentina|colombia|chile|costa rica|uae|egypt)\b", re.I)
_PROVINCE = re.compile(r",\s*(ON|BC|QC|AB|MB|NS|NL|PE|SK)\b")


def _has_term(text: str, terms: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(t.lower())}\b", text) for t in terms)


def outside_us(locations: list[str]) -> bool:
    locs = [l for l in locations if l and l.strip()]
    return bool(locs) and all((_FOREIGN.search(l) or _PROVINCE.search(l)) and not re.search(r"\b(usa?|united states)\b", l, re.I)
                              for l in locs)


def reject_reason(job: Job, settings: dict) -> str | None:
    """Return why a job is filtered out, or None if it should be applied to."""
    f = settings.get("filters", {})
    title = job.title.lower()

    max_age = settings.get("max_job_age_hours")
    if max_age and job.posted_at and job.posted_at < time.time() - max_age * 3600:
        return "too old"
    for terms in f.get("title_require_all") or []:
        if not _has_term(title, terms):
            return "title not included"
    if _has_term(title, f.get("title_exclude", [])):
        return "title excluded"
    if job.category and f.get("categories") and job.category not in f["categories"]:
        return f"category {job.category}"
    haystack = f"{job.company} {job.url}".lower()
    if any(c.lower() in haystack for c in f.get("exclude_companies", [])):
        return "company excluded"
    if f.get("us_only") and outside_us(job.locations):
        return "outside US"
    if f.get("locations_include") and job.locations:
        locs = " ".join(job.locations).lower()
        if not any(l.lower() in locs for l in f["locations_include"]):
            return "location"
    if f.get("needs_sponsorship") and job.sponsorship == "Does Not Offer Sponsorship":
        return "no sponsorship"
    if not f.get("us_citizen", True) and job.sponsorship == "U.S. Citizenship is Required":
        return "citizenship required"
    return None
