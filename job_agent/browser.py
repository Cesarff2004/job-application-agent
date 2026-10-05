"""Reads, fills and submits Greenhouse / Lever / Ashby application forms with Playwright.

The form is read generically (labels, types, options) rather than with per-site
selectors, so it keeps working when an ATS tweaks its markup. Anything the agent
can't fill confidently stops the job and sends it to the review queue.
"""
import difflib
import logging
import re
import time
from dataclasses import dataclass, field, replace

from playwright.sync_api import Page

from . import ats
from .answers import Answerer, rule_answer
from .config import ROOT
from .models import Job
from .sources.email_alerts import wait_for_code
from .state import DRY_RUN, FAILED, MANUAL, NEEDS_REVIEW, SUBMITTED

log = logging.getLogger(__name__)
SCREENSHOTS = ROOT / "screenshots"


@dataclass
class Result:
    status: str
    detail: str = ""
    answers: list = field(default_factory=list)


# Tags every fillable control with data-ja="<id>" and describes it.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const textOf = el => clean(el && el.innerText);
  const labelOf = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = clean(by.split(/\s+/).map(i => textOf(document.getElementById(i))).join(' '));
      if (t) return t;
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const wrap = el.closest('label');
    if (wrap) {
      // A <select> inside its <label> would otherwise drag all its option text along.
      const t = clean(textOf(wrap).replace(el.tagName === 'SELECT' ? textOf(el) : '', ''));
      if (t) return t;
    }
    return precedingText(el) || clean(el.placeholder || el.name || '');
  };
  // Nearest text before the control (Lever/Ashby put questions in plain divs, not <label>s).
  const precedingText = el => {
    let node = el;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.matches('input, select, textarea') || s.querySelector('input, select, textarea')) return '';
        const t = textOf(s);
        if (t) return t.slice(0, 400);
      }
    }
    return '';
  };
  // Question text for a radio/checkbox/button group: legend, else the surrounding text minus the options.
  const groupLabel = (els, optionTexts) => {
    const fs = els[0].closest('fieldset');
    if (fs && fs.querySelector('legend')) return textOf(fs.querySelector('legend'));
    let p = els[0].parentElement;
    while (p && !els.every(e => p.contains(e))) p = p.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      let t = textOf(p);
      for (const o of optionTexts) t = t.replace(o, '');
      t = clean(t);
      if (t.length > 2) return t.slice(0, 400);
    }
    return '';
  };

  const fields = [];
  let n = 0;
  const tag = el => { const id = 'f' + (n++); el.setAttribute('data-ja', id); return id; };
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label);
  const groups = {};

  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (/recaptcha|captcha|honeypot/i.test(el.name + el.id + el.className)) continue;
    // react-select's invisible "required" proxy inputs duplicate the real combobox.
    if (type !== 'file' && (el.getAttribute('aria-hidden') === 'true' || el.tabIndex === -1)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const key = type + ':' + (el.name || el.id);
      (groups[key] = groups[key] || []).push(el);
      continue;
    }
    if (type !== 'file' && !shown(el)) continue;
    const label = labelOf(el);
    const f = {id: tag(el), label: label.replace(/[*✱]/g, '').trim(), required: isRequired(el, label)};
    if (type === 'file') f.kind = 'file';
    else if (el.tagName === 'SELECT') {
      f.kind = 'select';
      f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t));
    } else if (el.getAttribute('role') === 'combobox') f.kind = 'combobox';
    else if (type === 'number' || type === 'date') f.kind = type;
    else f.kind = el.tagName === 'TEXTAREA' ? 'textarea' : 'text';
    f.value = el.value || '';
    fields.push(f);
  }

  for (const [key, els] of Object.entries(groups)) {
    const kind = key.split(':')[0];
    const opts = els.map(e => ({el: e, text: labelOf(e)}));
    if (kind === 'checkbox' && els.length === 1) {
      // A lone checkbox ("I agree", "I acknowledge") is a yes/no question.
      const label = opts[0].text;
      fields.push({id: tag(els[0]), kind: 'checkbox', label: label.replace(/[*✱]/g, '').trim(),
                   required: isRequired(els[0], label), options: ['Yes', 'No']});
      continue;
    }
    const label = groupLabel(els, opts.map(o => o.text));
    fields.push({
      id: 'g' + (n++), kind: kind === 'radio' ? 'radio' : 'checkbox_group',
      label: label.replace(/[*✱]/g, '').trim(),
      required: els.some(e => e.required || e.getAttribute('aria-required') === 'true') || /[*✱]/.test(label),
      options: opts.map(o => o.text), option_ids: opts.map(o => tag(o.el)),
    });
  }

  // Ashby-style Yes/No button pairs that have no underlying input.
  for (const box of document.querySelectorAll('div')) {
    const btns = [...box.children].filter(c => c.tagName === 'BUTTON');
    if (btns.length !== 2 || box.querySelector('[data-ja]')) continue;
    const texts = btns.map(textOf);
    if (texts.join('/').toLowerCase() !== 'yes/no') continue;
    const label = groupLabel(btns, texts);
    fields.push({id: 'g' + (n++), kind: 'radio', label: label.replace(/[*✱]/g, '').trim(),
                 required: /[*✱]/.test(label), options: texts, option_ids: btns.map(tag)});
  }
  return fields;
}
"""

# Index of the resume upload among the page's file inputs: the one whose surrounding text says
# resume/CV, else the first one that isn't for a cover letter. -1 if there is none.
RESUME_INPUT_JS = r"""
() => {
  const inputs = [...document.querySelectorAll('input[type=file]')];
  const near = el => {
    for (let p = el.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
      const t = (p.innerText || '').trim();
      if (t) return t.slice(0, 200).toLowerCase();
    }
    return '';
  };
  const texts = inputs.map(el => `${el.name} ${el.id} ${el.getAttribute('aria-label') || ''} ${near(el)}`.toLowerCase());
  const resume = texts.findIndex(t => /resume|\bcv\b/.test(t));
  return resume >= 0 ? resume : texts.findIndex(t => !/cover/.test(t));
}
"""

CONFIRM_RE = re.compile(
    r"thank(s| you) for (applying|your (application|interest|submission))|"
    r"application (has been |was )?(successfully )?(submitted|received)|"
    r"we('ve| have) received your application", re.I)


def best_option(value: str, options: list[str], fuzzy: bool = True) -> str | None:
    if not options or not value:
        return None
    v = value.strip().lower()
    lowered = {o.lower(): o for o in options}
    if v in lowered:
        return lowered[v]
    for o in options:
        if o.lower().startswith(v) or v.startswith(o.lower()):
            return o
    for o in options:
        if v in o.lower():
            return o
    if not fuzzy:  # e.g. school names: "State University of X" must never stand in for "X State University"
        return None
    match = difflib.get_close_matches(v, list(lowered), n=1, cutoff=0.6)
    return lowered[match[0]] if match else None


def _loc(page: Page, ja_id: str):
    return page.locator(f'[data-ja="{ja_id}"]').first


def _open_application(page: Page, job: Job) -> str:
    """Navigate to the actual form (following ATS iframes on company career sites). Returns the ATS name."""
    page.goto(job.url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    name = ats.detect(page.url)
    if name not in ats.SUPPORTED:
        for frame in page.frames:
            if ats.detect(frame.url) in ats.SUPPORTED:
                page.goto(frame.url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                name = ats.detect(page.url)
                break
    if name not in ats.SUPPORTED or name == "workday":
        return name

    if not job.description:
        job.description = page.inner_text("body")[:15000]
    if name == "lever" and not page.url.rstrip("/").endswith("/apply"):
        page.goto(page.url.split("?")[0].rstrip("/") + "/apply", wait_until="domcontentloaded")
    elif name == "ashby" and "/application" not in page.url:
        page.goto(page.url.split("?")[0].rstrip("/") + "/application", wait_until="domcontentloaded")
    else:
        apply_btn = page.get_by_role("button", name=re.compile(r"^apply", re.I))
        if apply_btn.count() and apply_btn.first.is_visible():
            apply_btn.first.click()
    page.wait_for_selector("input[type=file], form input, form textarea", timeout=30000)
    page.wait_for_timeout(1500)
    return name


def _listbox_options(page: Page, el):
    """Options of the dropdown this combobox opened (not some other open menu)."""
    owned = el.get_attribute("aria-controls") or el.get_attribute("aria-owns")
    if owned:
        return page.locator(f'[id="{owned}"] [role=option]')
    return page.locator("[role=option]:visible")


def _close_menu(page: Page, el):
    el.press("Escape")
    page.mouse.click(5, 5)
    page.wait_for_timeout(200)


def _combobox_options(page: Page, el) -> list[str]:
    try:
        el.click(timeout=5000)
        page.wait_for_timeout(600)
        opts = [t.strip() for t in _listbox_options(page, el).all_inner_texts()][:80]
        _close_menu(page, el)
        return [o for o in opts if o]
    except Exception:
        return []


def _fill(page: Page, f: dict, values: list[str], profile: dict):
    kind, value = f["kind"], (values[0] if values else "")
    if kind == "file":
        _loc(page, f["id"]).set_input_files(value)
    elif kind in ("text", "textarea", "number", "date"):
        _loc(page, f["id"]).fill(value)
    elif kind == "select":
        _loc(page, f["id"]).select_option(label=best_option(value, f["options"]))
    elif kind == "combobox":
        el = _loc(page, f["id"])
        el.click()
        el.fill(value)
        page.wait_for_timeout(1500)  # autocomplete (e.g. location) results load async
        options = _listbox_options(page, el)
        texts = options.all_inner_texts()
        pick = best_option(value, texts, fuzzy=False) or best_option("Other", texts, fuzzy=False)
        if pick is not None:
            options.nth(texts.index(pick)).click()
        elif not texts and not f.get("options"):
            page.keyboard.press("Enter")  # free-text combobox
        else:
            _close_menu(page, el)
            raise ValueError(f"no option matching {value!r}")
    elif kind == "checkbox":
        el = _loc(page, f["id"])
        want = value.lower() in ("yes", "true", "1", "i agree", "agree")
        if el.is_checked() != want:
            el.check(force=True) if want else el.uncheck(force=True)
    elif kind in ("radio", "checkbox_group"):
        for v in values if kind == "checkbox_group" else [value]:
            opt = best_option(v, f["options"])
            if opt is not None:
                _loc(page, f["option_ids"][f["options"].index(opt)]).click(force=True)


def _plan(page: Page, job: Job, profile: dict, answerer: Answerer, min_conf: float):
    """Decide a value for every field. Returns (plan, problems) where plan is [(field, values, source)]."""
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:
        if f["kind"] == "combobox":
            f["options"] = _combobox_options(page, _loc(page, f["id"]))

    plan, ask = [], []
    for f in fields:
        label = f["label"].lower()
        if f["kind"] == "file":
            if re.search(r"resume|\bcv\b", label) or not label:
                plan.append((f, [str(ROOT / profile["resume_path"])], "profile"))
            elif "cover" in label and profile.get("cover_letter_path"):
                plan.append((f, [str(ROOT / profile["cover_letter_path"])], "profile"))
            elif f["required"]:
                plan.append((f, [], "missing"))
            continue
        fixed = rule_answer(f["label"], profile)
        if fixed and f.get("options"):
            if f["kind"] != "combobox":
                fixed = best_option(fixed, f["options"])
            elif len(f["options"]) < 80:  # complete list (long ones like schools load as you type)
                fixed = best_option(fixed, f["options"], fuzzy=False)
        if fixed:
            plan.append((f, [fixed], "rule"))
        elif f.get("value") and not f["required"]:
            continue  # already filled (e.g. Ashby autofill from resume)
        else:
            ask.append(f)

    if ask:
        answers = answerer.answer(job, ask)
        for f in ask:
            a = answers.get(f["id"], {"value": [], "confidence": 0})
            values = [v for v in a["value"] if v]
            if f.get("options") and f["kind"] != "combobox":
                values = [o for o in (best_option(v, f["options"]) for v in values) if o]
            ok = values and a["confidence"] >= min_conf
            plan.append((f, values if ok else [], f"claude:{a['confidence']:.2f}" if ok else "unsure"))

    problems = [f["label"] or f["id"] for f, values, _ in plan if f["required"] and not values]
    return plan, problems


def _submit(page: Page) -> Result:
    started = time.time()
    button = page.get_by_role("button", name=re.compile(r"submit|send application|^apply$", re.I))
    if not button.count():
        button = page.locator("button[type=submit], input[type=submit]")
    button.first.click()

    for _ in range(3):  # submit, then possibly a verification code round-trip
        for _ in range(25):
            page.wait_for_timeout(1000)
            body = page.inner_text("body")
            if CONFIRM_RE.search(body) or re.search(r"confirmation|thank", page.url, re.I):
                return Result(SUBMITTED, "confirmed")
            challenge = page.locator("iframe[src*='recaptcha/api2/bframe'], iframe[src*='hcaptcha.com'][title*=challenge i]")
            if any(challenge.nth(i).is_visible() for i in range(challenge.count())):
                return Result(NEEDS_REVIEW, "CAPTCHA challenge; apply manually")
            code_box = page.locator("input[name*=code i], input[id*=code i], input[autocomplete=one-time-code]")
            if re.search(r"(security|verification) code", body, re.I) and code_box.count():
                code = wait_for_code(started)
                if not code:
                    return Result(NEEDS_REVIEW, "ATS emailed a verification code; set IMAP secrets to automate")
                if code_box.count() > 1:  # one box per character
                    code_box.first.click()
                    page.keyboard.type(code, delay=80)
                else:
                    code_box.first.fill(code)
                page.get_by_role("button", name=re.compile(r"submit|verify|confirm", re.I)).first.click()
                break
        else:
            errors = page.locator("[aria-invalid=true], .error, [class*=error i]").all_inner_texts()
            errors = "; ".join(e.strip() for e in errors if e.strip())[:300]
            return Result(FAILED, f"no confirmation after submit. {errors}".strip())
    return Result(FAILED, "verification loop did not finish")


def apply(page: Page, job: Job, profile: dict, answerer: Answerer, settings: dict, submit: bool) -> Result:
    SCREENSHOTS.mkdir(exist_ok=True)
    if submit and not settings.get("allow_submit", False):
        log.info("allow_submit is off in settings.yaml; filling without submitting")
        submit = False
    if ats.detect(job.url) == "workday":
        from . import workday
        return workday.apply(page, job, profile, answerer, settings, submit)
    name = _open_application(page, job)
    if name == "workday":  # company career page that redirects to / embeds Workday
        from . import workday
        return workday.apply(page, replace(job, url=page.url), profile, answerer, settings, submit)
    if name not in ats.SUPPORTED:
        return Result(MANUAL, f"{name} is not automated yet")

    # Upload the resume first: Ashby/Greenhouse parse it and prefill fields. Pick the resume box,
    # not just the first one, which on some forms is the cover letter.
    index = page.evaluate(RESUME_INPUT_JS)
    if index >= 0:  # nth(-1) would mean the last input, so check first
        page.locator("input[type=file]").nth(index).set_input_files(str(ROOT / profile["resume_path"]))
        page.wait_for_timeout(4000)

    plan, problems = _plan(page, job, profile, answerer, settings["claude"].get("min_confidence", 0.8))
    record = [{"q": f["label"], "a": v, "src": src} for f, v, src in plan if f["kind"] != "file"]
    shot = lambda: page.screenshot(path=str(SCREENSHOTS / f"{job.key}.png"), full_page=True)
    if problems:
        return Result(NEEDS_REVIEW, "couldn't answer: " + " | ".join(problems)[:400], record)

    for f, values, _ in plan:
        if not values:
            continue
        try:
            _fill(page, f, values, profile)
        except Exception as e:
            if f["required"]:
                shot()
                return Result(NEEDS_REVIEW, f"couldn't fill '{f['label']}': {str(e).splitlines()[0]}"[:300], record)
            log.warning("skipped optional field %s: %s", f["label"], str(e).splitlines()[0])

    shot()
    if not submit:
        return Result(DRY_RUN, f"filled {len(plan)} fields; not submitted", record)
    result = _submit(page)
    page.screenshot(path=str(SCREENSHOTS / f"{job.key}-after.png"), full_page=True)
    result.answers = record
    return result
