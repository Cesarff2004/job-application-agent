"""Answers application questions: fixed rules first, Claude for the rest."""
import json
import re
from datetime import date

import anthropic
import yaml

SYSTEM = """You fill out job applications on behalf of the applicant described in the profile.

Rules:
- Use ONLY facts in the profile. Never invent experience, skills, dates, numbers, or credentials.
- If the profile doesn't contain what a question needs, return an empty value with confidence 0.
  A human will answer it. Leaving it blank is always better than a guess.
- For fields with options, every value must be copied exactly from that field's options.
- For voluntary EEO / demographic questions, use the profile's eeo answers, matched to the closest option.
- Long-answer questions ("why this company", cover letter, describe a project): write in the applicant's
  first-person voice, specific to this job, drawn from the profile's about_me. Keep it under 150 words
  unless the question asks for more. Plain, sincere, no clichés, no em dashes.
- Confidence: 1.0 = directly stated in the profile, 0.8 = clearly implied, below 0.5 = you are guessing.
- value is a list: one item for ordinary fields, several only for multi-select checkbox groups."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "value": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["field_id", "value", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}


def _p(profile: dict, path: str) -> str:
    cur = profile
    for part in path.split("."):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    if isinstance(cur, bool):
        return "Yes" if cur else "No"
    return str(cur or "")


# (question regex, profile path). First match wins, so order matters. Only unambiguous
# facts belong here: yes/no questions are phrased too many ways ("authorized to work
# without sponsorship?") for a regex to answer safely, so Claude handles those.
RULES = [
    (r"preferred (first )?name", "preferred_name"),
    (r"middle name", "middle_name"),
    (r"first name|given name", "first_name"),
    (r"last name|surname|family name", "last_name"),
    (r"e-?mail", "email"),
    (r"device type", "phone_device_type"),
    (r"extension|country (phone )?code|phone code", "_none"),  # Claude picks from the options
    (r"phone|mobile", "phone"),
    (r"linkedin", "links.linkedin"),
    (r"github", "links.github"),
    (r"portfolio|personal (web)?site|website", "links.portfolio"),
    (r"\bschool\b|university|college", "education.school"),
    (r"\bdegree\b", "education.degree"),
    (r"discipline|major|field of study", "education.major"),
    (r"\bgpa\b", "education.gpa"),
    (r"\bzip|postal code", "location.zip"),
    (r"address line 1|street address", "location.address_line1"),
    (r"^city\b", "location.city"),
    (r"^state\b", "location.state"),
    (r"^country\b", "location.country"),
]

# Whole questions with a fixed answer. Checked before the field-name rules, so
# "How did you hear about us? (e.g. LinkedIn)" isn't answered with a LinkedIn URL.
QUESTION_RULES = [
    (r"how did you (hear|find|learn)", "preferences.how_did_you_hear"),
    (r"relocat", "preferences.willing_to_relocate"),
]

# The field-name rules only fit labels that name a fact ("LinkedIn Profile", "What is your GPA?"),
# not questions that merely mention one ("Have you used GitHub?", "Describe a project at your university").
_QUESTION = re.compile(
    r"^(have|has|had|do|does|did|are|is|were|was|will|would|can|could|should|describe|tell|explain|why|"
    r"share|list|please (describe|explain|tell|share|list)|"
    r"what (interests|excites|attracts|draws|motivates|makes)|how (would|do|have|will|does))\b"
    r"|\b(describe|explain|tell us|why)\b")


def _names_a_fact(text: str) -> bool:
    return len(text.split()) <= 8 and not _QUESTION.search(text)


def rule_answer(label: str, profile: dict) -> str | None:
    text = label.lower().strip()
    for item in profile.get("custom_answers") or []:
        if re.search(item["match"], text, re.I):
            return str(item["answer"])
    for pattern, path in QUESTION_RULES:
        if re.search(pattern, text):
            return _p(profile, path) or None
    if not _names_a_fact(text):
        return None  # Claude answers it, with a confidence score
    if re.search(r"full name|^name\b|^your name", text):
        return f"{profile['first_name']} {profile['last_name']}"
    if re.search(r"^(current )?location|where are you (located|based)", text):
        loc = profile.get("location", {})
        return ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
    for pattern, path in RULES:
        if re.search(pattern, text):
            return _p(profile, path) or None
    return None


class Answerer:
    def __init__(self, profile: dict, settings: dict):
        self.profile = profile
        cfg = settings.get("claude", {})
        self.model = cfg.get("model", "claude-opus-5-5")
        self.effort = cfg.get("effort", "medium")
        self.client = anthropic.Anthropic()
        # Resume path and custom rules are not useful to the model.
        shown = {k: v for k, v in profile.items() if k not in ("resume_path", "cover_letter_path", "custom_answers")}
        self.profile_text = yaml.safe_dump(shown, sort_keys=False, allow_unicode=True)

    def answer(self, job, fields: list[dict]) -> dict[str, dict]:
        """Returns {field_id: {"value": [...], "confidence": float}} for the given fields."""
        if not fields:
            return {}
        questions = [
            {k: f[k] for k in ("id", "label", "kind", "required", "options") if f.get(k) not in (None, [], "")}
            for f in fields
        ]
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": SYSTEM},
                    {"type": "text", "text": f"<profile>\n{self.profile_text}</profile>",
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content":
                f"Today's date: {date.today().isoformat()}\n"
                f"<job company={json.dumps(job.company)} title={json.dumps(job.title)}>\n"
                f"{job.description[:12000]}\n</job>\n\n"
                f"Answer every one of these application fields:\n{json.dumps(questions, indent=1)}"}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"Claude stopped with {response.stop_reason}")
        text = next(b.text for b in response.content if b.type == "text")
        return {a["field_id"]: a for a in json.loads(text)["answers"]}
