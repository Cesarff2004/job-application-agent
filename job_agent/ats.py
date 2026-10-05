import re

SUPPORTED = ("greenhouse", "lever", "ashby", "workday")

_PATTERNS = [
    ("greenhouse", r"greenhouse\.io|[?&]gh_jid="),
    ("lever", r"jobs\.(eu\.)?lever\.co"),
    ("ashby", r"ashbyhq\.com|[?&]ashby_jid="),
    ("workday", r"myworkdayjobs\.com|myworkdaysite\.com"),
    ("icims", r"icims\.com"),
    ("oracle", r"oraclecloud\.com"),
    ("smartrecruiters", r"smartrecruiters\.com"),
    ("jobvite", r"jobvite\.com"),
    ("workable", r"workable\.com"),
    ("successfactors", r"successfactors|jobs\.sap\.com"),
    ("linkedin", r"linkedin\.com"),
    ("indeed", r"indeed\.com"),
]


def detect(url: str) -> str:
    for name, pattern in _PATTERNS:
        if re.search(pattern, url, re.I):
            return name
    return "other"
