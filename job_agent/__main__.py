"""
python -m job_agent run [--mode auto|dry_run]   one polling cycle (what GitHub Actions runs)
python -m job_agent discover                    list new matching jobs without applying
python -m job_agent apply <url> [--submit] [--headed]   try one posting, for testing
python -m job_agent inbox                       update application stages from employer emails
python -m job_agent check                       test the API key, Gmail and phone notifications
"""
import argparse
import logging
import os
import re
import sys

from .config import load_settings


# Secrets pasted into GitHub can carry a trailing newline, spaces or quotes. In the API key that
# makes an illegal HTTP header, and every Claude call fails. Clean them before anything reads them.
# ATS_PASSWORD is left exactly as entered: it must match the accounts already created with it.
_CLEAN = ("ANTHROPIC_API_KEY", "NTFY_TOPIC", "IMAP_USER", "IMAP_PASSWORD")


def _clean_secrets():
    for name in _CLEAN:
        if name in os.environ:
            os.environ[name] = os.environ[name].strip().strip("\"'").strip()
    # A block of several secrets pasted into the key's box: keep just the key.
    if m := re.search(r"sk-ant-[A-Za-z0-9_-]+", os.environ.get("ANTHROPIC_API_KEY", "")):
        os.environ["ANTHROPIC_API_KEY"] = m.group(0)


def _describe_key(key: str) -> str:
    """What's wrong with the key secret, without revealing anything about what's in it."""
    extra = any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in key)
    return (f"{'contains' if re.search(r'sk-ant-', key) else 'does NOT contain'} an sk-ant- key; "
            f"{'has other text around it (labels, spaces or line breaks)' if extra else 'nothing else'}")


def _check(settings: dict) -> bool:
    """One small request to each service the agent depends on. Touches no jobs."""
    ok = True
    try:
        import anthropic
        anthropic.Anthropic().messages.create(
            model=settings.get("claude", {}).get("model", "claude-opus-5-5"), max_tokens=64,
            output_config={"effort": "low"}, messages=[{"role": "user", "content": "Reply with OK."}])
        print("Claude API key: OK")
    except Exception as e:
        ok = False
        cause = e.__cause__ or e.__context__
        print(f"Claude API key: FAILED ({type(e).__name__}: {e})" + (f" caused by {type(cause).__name__}: {cause}" if cause else ""))
        print(f"  key shape: {_describe_key(os.environ.get('ANTHROPIC_API_KEY', ''))}")
    if os.environ.get("IMAP_USER"):
        try:
            from .sources.email_alerts import _connect
            _connect().logout()
            print("Gmail (IMAP): OK")
        except Exception as e:
            ok = False
            print(f"Gmail (IMAP): FAILED ({type(e).__name__}: {e})")
    else:
        print("Gmail (IMAP): not set up")
    if os.environ.get("NTFY_TOPIC"):
        from . import notify
        notify.send("Job agent check", "Setup check ran. If you see this, notifications work.", tags="white_check_mark")
        print("Phone notification: sent (check your phone)")
    else:
        ok = False
        print("Phone notification: NTFY_TOPIC is not set")
    return ok


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # job titles/questions contain non-ASCII; Windows consoles choke
    _clean_secrets()
    parser = argparse.ArgumentParser(prog="job_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--mode", choices=["auto", "dry_run"])
    sub.add_parser("discover")
    sub.add_parser("inbox")
    sub.add_parser("check")
    one = sub.add_parser("apply")
    one.add_argument("url")
    one.add_argument("--submit", action="store_true")
    one.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()

    if args.cmd == "run":
        from .pipeline import run
        if args.mode:
            settings["mode"] = args.mode
        run(settings)

    elif args.cmd == "discover":
        from .pipeline import new_jobs
        from .state import State
        auto, manual = new_jobs(settings, State())
        for tag, jobs in (("AUTO", auto), ("MANUAL", manual)):
            for j in jobs:
                print(f"{tag:6} {j.label()}  {j.url}")
        print(f"\n{len(auto)} automatable, {len(manual)} manual")
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a") as f:
                f.write(f"auto={len(auto)}\nmanual={len(manual)}\n")

    elif args.cmd == "check":
        sys.exit(0 if _check(settings) else 1)

    elif args.cmd == "inbox":
        from .inbox import check
        check(settings)

    elif args.cmd == "apply":
        from playwright.sync_api import sync_playwright

        from . import browser
        from .answers import Answerer
        from .config import load_profile
        from .models import Job
        profile = load_profile()
        job = Job(company="(manual test)", title="(manual test)", url=args.url, source="cli")
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=not args.headed)
            page = b.new_page(viewport={"width": 1280, "height": 1600})
            result = browser.apply(page, job, profile, Answerer(profile, settings), settings, submit=args.submit)
            print(result.status, result.detail)
            for a in result.answers:
                print(f"  [{a['src']}] {a['q'][:70]!r} -> {a['a']}")
            if args.headed:
                input("Press Enter to close the browser...")
            b.close()


if __name__ == "__main__":
    main()
