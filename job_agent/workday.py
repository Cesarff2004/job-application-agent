"""Workday (myworkdayjobs.com) applications: account sign-up/sign-in, then the multi-step form.

Every Workday tenant needs its own candidate account. The first time the agent applies
to a company it creates one with your email and ATS_PASSWORD; after that it signs in
with the same account. Each step of the form is read generically (Workday's
data-automation-id attributes are stable) and answered like the other ATSes.
"""
import logging
import re
import time
from datetime import date

from playwright.sync_api import Page

from .accounts import Accounts, password
from .answers import Answerer, rule_answer
from .browser import CONFIRM_RE, SCREENSHOTS, Result, best_option
from .config import ROOT
from .models import Job
from .sources.email_alerts import wait_for_link
from .state import DRY_RUN, FAILED, NEEDS_REVIEW, SUBMITTED

log = logging.getLogger(__name__)
# Detail prefix for jobs waiting on account verification; the pipeline retries these next run.
PENDING_VERIFY = "account verification pending"

WD_CONFIRM_RE = re.compile(r"application submitted|successfully submitted|you('ve| have) (successfully )?applied", re.I)
VERIFY_RE = re.compile(r"verify your (e-?mail|account)|verification (e-?mail|link)|check your (e-?mail|inbox)|"
                       r"(e-?mail|link) (has been|was) sent", re.I)
NEXT = ("[data-automation-id=pageFooterNextButton], [data-automation-id=bottom-navigation-next-button]")

# Reads the current step. Each question sits in a data-automation-id="formField-*" wrapper.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const textOf = el => clean(el && el.innerText);
  let n = 0;
  const tag = el => { const id = 'w' + (n++); el.setAttribute('data-ja', id); return id; };
  const groupOf = el => {
    const g = el.closest('[role=group][aria-labelledby]');
    if (!g) return '';
    return clean(g.getAttribute('aria-labelledby').split(/\s+/).map(i => textOf(document.getElementById(i))).join(' '));
  };
  const optionLabel = el => {
    const l = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return textOf(l) || clean(el.getAttribute('aria-label')) || textOf(el.closest('label')) || clean(el.value);
  };
  const fields = [];
  const done = new Set();
  const wrappers = [...document.querySelectorAll('[data-automation-id^="formField-"]')]
    .filter(w => shown(w) && !w.querySelector('[data-automation-id^="formField-"]'));
  for (const w of wrappers) {
    const lab = w.querySelector('label, legend');
    const raw = textOf(lab) || clean(w.getAttribute('data-automation-id').replace('formField-', ''));
    const f = {label: raw.replace(/[*✱]/g, '').trim(), group: groupOf(w),
               required: /[*✱]/.test(raw) || !!w.querySelector('[aria-required=true], [required]')};
    const date = w.querySelectorAll('[data-automation-id^="dateSection"][data-automation-id$="-input"]');
    const dropdown = w.querySelector('button[aria-haspopup=listbox]');
    const prompt = w.querySelector('[data-uxi-widget-type=selectinput], input[data-automation-id=searchBox]');
    const radios = [...w.querySelectorAll('input[type=radio]')];
    const boxes = [...w.querySelectorAll('input[type=checkbox]')];
    const file = w.querySelector('input[type=file]');
    const text = w.querySelector('textarea, input[type=text], input[type=email], input[type=tel], input[type=number], input:not([type])');
    if (date.length) {
      f.kind = 'date';
      f.parts = [...date].map(d => d.getAttribute('data-automation-id').replace('dateSection', '').replace('-input', '').toLowerCase());
      f.part_ids = [...date].map(tag);
      f.value = [...date].map(d => d.value || d.getAttribute('aria-valuetext') || '').join('/').replace(/^\/+|\/+$/g, '');
      f.id = f.part_ids[0];
    } else if (dropdown) {
      f.kind = 'dropdown'; f.id = tag(dropdown);
      f.value = /^select one$/i.test(textOf(dropdown)) ? '' : textOf(dropdown);
    } else if (prompt) {
      f.kind = 'prompt'; f.id = tag(prompt);
      f.value = [...w.querySelectorAll('[data-automation-id=selectedItem]')].map(textOf).join('; ');
    } else if (radios.length) {
      f.kind = 'radio'; f.options = radios.map(optionLabel); f.option_ids = radios.map(tag); f.id = 'g' + (n++);
      f.value = (radios.find(r => r.checked) && optionLabel(radios.find(r => r.checked))) || '';
    } else if (boxes.length > 1) {
      f.kind = 'checkbox_group'; f.options = boxes.map(optionLabel); f.option_ids = boxes.map(tag); f.id = 'g' + (n++);
      f.value = boxes.filter(b => b.checked).map(optionLabel).join('; ');
    } else if (boxes.length === 1) {
      f.kind = 'checkbox'; f.id = tag(boxes[0]); f.options = ['Yes', 'No'];
      if (!f.label || f.label === optionLabel(boxes[0])) f.label = optionLabel(boxes[0]) || f.label;
      f.value = boxes[0].checked ? 'Yes' : '';
    } else if (file) {
      f.kind = 'file'; f.id = tag(file);
    } else if (text) {
      if (text.getAttribute('data-automation-id') === 'beecatcher' || text.readOnly || text.disabled) continue;
      f.kind = text.tagName === 'TEXTAREA' ? 'textarea' : 'text'; f.id = tag(text); f.value = text.value || '';
    } else continue;
    w.querySelectorAll('input, textarea, button').forEach(e => done.add(e));
    fields.push(f);
  }
  // Consent checkboxes that live outside a formField wrapper (e.g. "I have read and agree").
  for (const box of document.querySelectorAll('input[type=checkbox]')) {
    if (done.has(box) || !shown(box.parentElement)) continue;
    const label = optionLabel(box) || textOf(box.closest('div'));
    fields.push({id: tag(box), kind: 'checkbox', options: ['Yes', 'No'], group: groupOf(box),
                 label: label.replace(/[*✱]/g, '').trim().slice(0, 400), value: box.checked ? 'Yes' : '',
                 required: box.getAttribute('aria-required') === 'true' || /[*✱]/.test(label)});
  }
  return fields;
}
"""


def _loc(page: Page, ja_id: str):
    return page.locator(f'[data-ja="{ja_id}"]').first


def _visible_options(page: Page):
    return page.locator("[role=listbox] [role=option]:visible, [data-automation-id=promptOption]:visible")


def _texts(loc) -> list[str]:
    return [t.strip() for t in loc.all_inner_texts()]


def _escape(page: Page):
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


def _wait_any(page: Page, selectors: list[str], timeout: float = 20) -> str | None:
    """Wait until one of the selectors is visible; return it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in selectors:
            loc = page.locator(s)
            if loc.count() and loc.first.is_visible():
                return s
        page.wait_for_timeout(500)
    return None


def _errors(page: Page) -> str:
    texts = page.locator("[data-automation-id=errorMessage]:visible, [data-automation-id=errorBanner]:visible, "
                         "[role=alert]:visible").all_inner_texts()
    return "; ".join(dict.fromkeys(t.strip().replace("\n", " ") for t in texts if t.strip()))[:400]


def _click_button(page: Page, automation_id: str):
    """Workday covers its submit buttons with a transparent click_filter div; click whichever is on top."""
    btn = page.locator(f"[data-automation-id={automation_id}]").first
    overlay = btn.locator("xpath=preceding-sibling::*[@data-automation-id='click_filter']")
    # It ignores synthetic click events; only a real press and release submits.
    (overlay.first if overlay.count() else btn).hover()
    page.mouse.down()
    page.mouse.up()


# ---------------------------------------------------------------- account

def _type(page: Page, automation_id: str, text: str):
    box = page.locator(f"[data-automation-id={automation_id}]").first
    box.click()
    box.fill("")
    box.press_sequentially(text, delay=25)

ON_FORM = ["[data-automation-id=progressBarActiveStep]:not(:has-text('Sign In'))",
           "input[type=file]", "[data-automation-id=applyFlowMyInfoPage]"]
SIGN_IN_FORM = "[data-automation-id=signInSubmitButton], [data-automation-id=createAccountSubmitButton]"


def _past_sign_in(page: Page) -> bool:
    step = page.locator("[data-automation-id=progressBarActiveStep]")
    return bool(step.count()) and not re.search(r"sign in|create account", step.first.inner_text(), re.I)


def _form_gone(page: Page) -> bool:
    """True once neither the sign-in nor the create-account form is on screen."""
    loc = page.locator(SIGN_IN_FORM)
    return not any(loc.nth(i).is_visible() for i in range(loc.count()))


def open_flow(page: Page, job: Job, returning: bool) -> str | None:
    """From the posting, click Apply and pick how to start. Returns an error, or None."""
    page.goto(job.url, wait_until="domcontentloaded", timeout=45000)
    if not _wait_any(page, ["[data-automation-id=adventureButton]"], 25):
        body = page.inner_text("body")
        if re.search(r"no longer (available|accepting)|page you are looking for doesn't exist", body, re.I):
            return "posting is closed"
        return "no Apply button on the posting"
    desc = page.locator("[data-automation-id=jobPostingDescription]")
    if desc.count() and not job.description:
        job.description = desc.first.inner_text()[:15000]
    page.locator("[data-automation-id=adventureButton]").first.click()
    # Returning applicants reuse their last application; new ones autofill from the resume.
    options = ["[data-automation-id=autofillWithResume]", "[data-automation-id=applyManually]"]
    if returning:
        options.insert(0, "[data-automation-id=useMyLastApplication]")
    choice = _wait_any(page, options + ["[data-automation-id=email]"] + ON_FORM, 20)
    if choice in options:
        page.locator(choice).first.click()
    _wait_any(page, ["[data-automation-id=email]", "[data-automation-id=createAccountLink]",
                     "[data-automation-id=signInLink]"] + ON_FORM, 30)
    return None


def _sign_in(page: Page, email: str) -> str | None:
    """Sign in on the current page. Returns an error string, or None on success."""
    if not page.locator("[data-automation-id=signInSubmitButton]").count():
        link = page.locator("[data-automation-id=signInLink]")
        if link.count():
            link.first.click()
            _wait_any(page, ["[data-automation-id=signInSubmitButton]"], 10)
    _type(page, "email", email)
    _type(page, "password", password())
    _click_button(page, "signInSubmitButton")
    for _ in range(30):
        page.wait_for_timeout(1000)
        if err := _errors(page):
            return err
        if _past_sign_in(page) or _form_gone(page):
            return None
    return "sign-in did not finish"


def _create_account(page: Page, email: str, started: float) -> str:
    """Returns "created", "exists", "verify", or an error message."""
    if not page.locator("[data-automation-id=createAccountSubmitButton]").count():
        link = page.locator("[data-automation-id=createAccountLink]")
        if not link.count():
            return "no Create Account option on this site"
        link.first.click()
        _wait_any(page, ["[data-automation-id=verifyPassword]"], 10)
    _type(page, "email", email)
    _type(page, "password", password())
    _type(page, "verifyPassword", password())
    agree = page.locator("[data-automation-id=createAccountCheckbox]")
    if agree.count() and not agree.first.is_checked():
        agree.first.check(force=True)
    _click_button(page, "createAccountSubmitButton")
    for _ in range(30):
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        if re.search(r"already (exists|in use|registered)", body, re.I):
            return "exists"
        if VERIFY_RE.search(body):
            return "created" if _verify(page, started) else "verify"
        if err := _errors(page):
            return err
        # Workday either continues the application or drops you on its own Sign In page.
        if _past_sign_in(page) or not page.locator("[data-automation-id=createAccountSubmitButton]:visible").count():
            return "created"
    return "account creation did not finish"


VERIFY_LINK = r"https://[^\s\"'<>]*myworkday[^\s\"'<>]*(?:verif|activat|confirm|token)[^\s\"'<>]*"


def _verify(page: Page, since: float) -> bool:
    """Open the account-verification link Workday emailed. Needs the IMAP secrets."""
    link = wait_for_link(since, VERIFY_LINK)
    if not link:
        return False
    tab = page.context.new_page()
    tab.goto(link, wait_until="domcontentloaded")
    tab.wait_for_timeout(4000)
    tab.close()
    return True


def ensure_signed_in(page: Page, job: Job, accounts: Accounts, email: str) -> str | None:
    """Sign in, creating the account the first time. Leaves the page inside the application."""
    if _past_sign_in(page):
        return None
    known = accounts.get(job.url)
    if not known:
        outcome = _create_account(page, email, time.time())
        if outcome == "verify":
            accounts.record(job.url, email, job.company, created_by_agent=True, unverified=True)
            return f"{PENDING_VERIFY}: open the email from {job.company} and click its link"
        if outcome not in ("created", "exists"):
            return f"account creation failed: {outcome}"
        accounts.record(job.url, email, job.company, created_by_agent=outcome == "created")
        if _past_sign_in(page):
            return None
    if not _past_sign_in(page) and not page.locator("[data-automation-id=signInSubmitButton], "
                                                    "[data-automation-id=signInLink]").count():
        open_flow(page, job, returning=bool(known))
    if not _past_sign_in(page):
        err = _sign_in(page, email)
        if err and re.search(r"verif", err, re.I):
            entry = accounts.get(job.url)
            if not _verify(page, entry.get("created", time.time() - 86400)):
                accounts.record(job.url, email, job.company, unverified=True)
                return f"{PENDING_VERIFY}: open the email from {job.company} and click its link"
            accounts.record(job.url, email, job.company, unverified=False)
            open_flow(page, job, returning=True)
            err = None if _past_sign_in(page) else _sign_in(page, email)
        if err:
            if not known:
                return (f"an account for {email} already exists here with a different password; "
                        f"reset it to ATS_PASSWORD on this site. ({err})")
            return f"sign-in failed: {err}"
    accounts.record(job.url, email, job.company)
    if not _past_sign_in(page):  # signed in from Workday's standalone page; go back to the application
        open_flow(page, job, returning=True)
    if not _past_sign_in(page):
        return "signed in, but the application form did not open"
    return None


# ---------------------------------------------------------------- filling

def _parse_date(value: str) -> dict:
    v = value.strip()
    if v.lower() in ("today", "now"):
        d = date.today()
        return {"month": f"{d.month:02d}", "day": f"{d.day:02d}", "year": str(d.year)}
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", v):
        return {"year": m[1], "month": m[2].zfill(2), "day": (m[3] or "01").zfill(2)}
    if m := re.fullmatch(r"(\d{1,2})/(?:(\d{1,2})/)?(\d{4})", v):
        return {"month": m[1].zfill(2), "day": (m[2] or "01").zfill(2), "year": m[3]}
    if m := re.fullmatch(r"(\d{4})", v):
        return {"year": m[1], "month": "01", "day": "01"}
    raise ValueError(f"unrecognized date {value!r}")


def _choose(page: Page, field: dict, value: str, job: Job, answerer: Answerer) -> bool:
    """Pick `value` from an open Workday list, following one level of sub-menus."""
    for depth in range(3):
        opts = _visible_options(page)
        texts = _texts(opts)
        pick = best_option(value, texts, fuzzy=False)
        if pick is None and depth > 0 and texts:
            # Clicked a category ("Job Board"); ask which of its items fits.
            sub = dict(field, label=f"{field['label']} > {value}", options=texts, kind="dropdown")
            a = answerer.answer(job, [sub]).get(sub["id"], {"value": []})
            pick = best_option((a["value"] or [""])[0], texts, fuzzy=False)
        if pick is None:
            return False
        before = texts
        opts.nth(texts.index(pick)).click()
        page.wait_for_timeout(1200)
        after = _texts(_visible_options(page))
        if not after or after == before or field["kind"] == "dropdown":
            return True
    return True


def _fill(page: Page, f: dict, values: list[str], job: Job, answerer: Answerer):
    kind, value = f["kind"], (values[0] if values else "")
    el = _loc(page, f["id"])
    if kind == "file":
        el.set_input_files(value)
        page.wait_for_timeout(4000)
    elif kind in ("text", "textarea"):
        el.fill(value)
        el.press("Tab")
    elif kind == "date":
        parts = _parse_date(value)
        for part, pid in zip(f["parts"], f["part_ids"]):
            box = _loc(page, pid)
            box.click()
            box.press_sequentially(parts[part], delay=60)
        page.keyboard.press("Tab")
    elif kind == "dropdown":
        el.click()
        page.wait_for_timeout(700)
        if not _choose(page, f, value, job, answerer):
            _escape(page)
            raise ValueError(f"no option matching {value!r}")
    elif kind == "prompt":
        for v in values:
            el.click()
            page.wait_for_timeout(900)
            if _texts(_visible_options(page)) and _choose(page, f, v, job, answerer):
                continue
            el.fill(v)  # searchable list (schools, fields of study, skills)
            el.press("Enter")
            page.wait_for_timeout(2000)
            if not _choose(page, f, v, job, answerer):
                _escape(page)
                raise ValueError(f"no option matching {v!r}")
        _escape(page)
    elif kind == "checkbox":
        want = value.lower() in ("yes", "true", "1", "i agree", "agree")
        if el.is_checked() != want:
            el.check(force=True) if want else el.uncheck(force=True)
    elif kind in ("radio", "checkbox_group"):
        for v in values if kind == "checkbox_group" else [value]:
            opt = best_option(v, f["options"])
            if opt is not None:
                _loc(page, f["option_ids"][f["options"].index(opt)]).click(force=True)


def _prompt_options(page: Page, f: dict) -> list[str]:
    try:
        _loc(page, f["id"]).click(timeout=5000)
        page.wait_for_timeout(800)
        opts = _texts(_visible_options(page))[:80]
        _escape(page)
        return [o for o in opts if o]
    except Exception:
        return []


def _add_missing_entries(page: Page, profile: dict):
    """With an image-only resume, autofill leaves Work Experience / Education empty. Add one block per entry."""
    wanted = {"Work Experience": len(profile.get("experience") or []),
              "Education": 1 + len(profile.get("past_education") or [])}
    for section, count in wanted.items():
        heading = page.locator(f"h3:has-text('{section}'), h4:has-text('{section}')").first
        if not heading.count():
            continue
        have = page.locator(f"[role=group][aria-labelledby]:has(h4:has-text('{section}'))").count()
        container = heading.locator("xpath=ancestor::*[.//button][1]")
        for _ in range(max(count - have, 0)):
            btn = container.get_by_role("button", name=re.compile(r"^add", re.I))
            if not btn.count():
                break
            btn.last.click()
            page.wait_for_timeout(1200)


def _plan_step(page: Page, job: Job, profile: dict, answerer: Answerer, min_conf: float):
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:
        if f["kind"] in ("dropdown", "prompt") and not f.get("value"):
            f["options"] = _prompt_options(page, f)
        if f["group"]:
            f["label"] = f"{f['group']} > {f['label']}"

    plan, ask = [], []
    for f in fields:
        if f["kind"] == "file":
            if re.search(r"resume|\bcv\b|upload|attach", f["label"], re.I) or not f["label"]:
                plan.append((f, [str(ROOT / profile["resume_path"])], "profile"))
            continue
        if f.get("value"):
            continue  # already filled by autofill or a previous application
        # Repeated blocks ("Education 2 > Degree") need context the regex rules lack.
        fixed = None if f["group"] else rule_answer(f["label"], profile)
        if fixed and f.get("options"):
            fixed = best_option(fixed, f["options"], fuzzy=f["kind"] != "prompt")
        if fixed:
            plan.append((f, [fixed], "rule"))
        else:
            ask.append(f)

    if ask:
        answers = answerer.answer(job, ask)
        for f in ask:
            a = answers.get(f["id"], {"value": [], "confidence": 0})
            values = [v for v in a["value"] if v]
            if f.get("options") and f["kind"] in ("radio", "checkbox_group", "dropdown"):
                values = [o for o in (best_option(v, f["options"]) for v in values) if o]
            ok = values and a["confidence"] >= min_conf
            plan.append((f, values if ok else [], f"claude:{a['confidence']:.2f}" if ok else "unsure"))

    problems = [f["label"] or f["id"] for f, values, _ in plan if f["required"] and not values]
    return plan, problems


def _step_name(page: Page) -> str:
    step = page.locator("[data-automation-id=progressBarActiveStep]")
    text = step.first.inner_text() if step.count() else ""
    return re.sub(r"^.*?step \d+ of \d+\s*", "", text.replace("\n", " "), flags=re.I).strip()


def apply(page: Page, job: Job, profile: dict, answerer: Answerer, settings: dict, submit: bool) -> Result:
    SCREENSHOTS.mkdir(exist_ok=True)
    shot = lambda suffix="": page.screenshot(path=str(SCREENSHOTS / f"{job.key}{suffix}.png"), full_page=True)
    accounts = Accounts()
    email = profile["email"]
    min_conf = settings["claude"].get("min_confidence", 0.8)

    if err := open_flow(page, job, returning=bool(accounts.get(job.url))):
        return Result(FAILED, err)
    if err := ensure_signed_in(page, job, accounts, email):
        shot()
        return Result(NEEDS_REVIEW, err)

    record, seen_steps = [], []
    for _ in range(12):
        page.wait_for_timeout(2500)
        step = _step_name(page)
        if WD_CONFIRM_RE.search(page.inner_text("body")):
            return Result(SUBMITTED, "confirmed", record)
        if seen_steps[-2:] == [step, step]:
            shot()
            return Result(NEEDS_REVIEW, f"stuck on '{step}': {_errors(page) or 'no error shown'}", record)
        seen_steps.append(step)
        log.info("workday step: %s", step)

        if re.search(r"review", step, re.I):
            shot()
            if not submit:
                return Result(DRY_RUN, f"reached Review after {len(seen_steps) - 1} steps; not submitted", record)
            page.locator(NEXT).first.click()  # labelled "Submit" on the Review step
            for _ in range(30):
                page.wait_for_timeout(1000)
                if WD_CONFIRM_RE.search(page.inner_text("body")) or CONFIRM_RE.search(page.inner_text("body")):
                    accounts.record(job.url, email, job.company, applied=accounts.get(job.url).get("applied", 0) + 1)
                    shot("-after")
                    return Result(SUBMITTED, "confirmed", record)
            shot("-after")
            return Result(FAILED, f"no confirmation after submit. {_errors(page)}".strip(), record)

        if re.search(r"experience", step, re.I):
            _add_missing_entries(page, profile)
        plan, problems = _plan_step(page, job, profile, answerer, min_conf)
        record += [{"q": f"[{step}] {f['label']}", "a": v, "src": src} for f, v, src in plan if f["kind"] != "file"]
        if problems:
            shot()
            return Result(NEEDS_REVIEW, f"[{step}] couldn't answer: " + " | ".join(problems)[:400], record)
        for f, values, _ in plan:
            if not values:
                continue
            try:
                _fill(page, f, values, job, answerer)
            except Exception as e:
                if f["required"]:
                    shot()
                    return Result(NEEDS_REVIEW, f"[{step}] couldn't fill '{f['label']}': {str(e).splitlines()[0]}"[:300], record)
                log.warning("skipped optional field %s: %s", f["label"], str(e).splitlines()[0])
        page.locator(NEXT).first.click()
    shot()
    return Result(FAILED, "too many steps", record)
