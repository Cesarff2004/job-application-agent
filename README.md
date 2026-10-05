# Job Agent

Watches job lists and company career boards and applies to matching jobs for you.
It runs on GitHub Actions every 30 minutes, so it keeps working while your PC is off.

## Using it yourself

This public repo is the code only: the profile is a blank template and there is no resume or job data.
The agent commits your applications and email-derived stages back to the repo, so **run your own copy
in a private repository**, not a fork (forks of a public repo can't be made private):

1. Create a new **private** repo, then clone this one and push it there
   (or use **Use this template → Create a new repository → Private**).
2. Fill in `config/profile.yaml`, add `config/resume.pdf`, and follow **Setup** below.
3. Set the repository variable `JOB_AGENT_ENABLED` to `true` (Settings → Secrets and variables →
   Actions → Variables). The scheduled run and the tracker deploy stay off until you do.

## What it does

1. **Finds jobs** from:
   - SimplifyJobs repos (internships and new grad), using their structured `listings.json`
   - any other GitHub repo with a README job table
   - Greenhouse, Lever, and Ashby boards of companies you list (catches postings before the lists do)
   - LinkedIn and Indeed job-alert **emails** in your inbox. It never logs into those sites. It looks up the company's own apply link and applies there.
2. **Filters** them by title, category, location, sponsorship, and age (`config/settings.yaml`).
3. **Applies** on Greenhouse, Lever, Ashby, and **Workday**:
   - reads every question on the form
   - fills obvious fields from your profile
   - asks Claude to answer the rest, **using only facts in `config/profile.yaml`**
   - submits and confirms that the "thank you" page appeared
   - on Workday, which needs an account per company, it creates one the first time (your email + `ATS_PASSWORD`) and signs in with it after that. `state/accounts.json` lists those sites; no password is stored there.
4. **Sends a phone notification** (ntfy) for each application and for anything that needs you.
5. **Tracks replies.** It reads new emails from companies you applied to and moves each application to Rejected, Assessment, Interview, or Offer (`state/inbox.json`). Interviews and offers also send a high-priority notification.

Jobs it **won't** submit go to [`state/review_queue.md`](state/review_queue.md) so you can apply by hand:
- iCIMS, Oracle, and other platforms it can't automate yet
- a Workday account waiting on you to click its verification email (retried every run until you do)
- a required question it couldn't answer confidently
- a CAPTCHA

It never guesses on a required question.

## Setup

1. **Keep this repo private.** It holds your resume and contact details.
2. Fill in `config/profile.yaml`. Put your resume at `config/resume.pdf`.
   The more real detail you put in `about_me`, the better the written answers will be.
3. Tune `config/settings.yaml`: filters, the companies whose boards to watch, and daily caps.
4. Install the [ntfy app](https://ntfy.sh) on your phone and subscribe to a hard-to-guess topic name, e.g. `yourname-jobs-8f3k2`.
5. Add the repo secrets under GitHub → Settings → Secrets and variables → Actions:

   | Secret | Needed | What |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | yes | from console.anthropic.com |
   | `NTFY_TOPIC` | yes | your ntfy topic name |
   | `ATS_PASSWORD` | for Workday | One password for every Workday account the agent creates. Keep a copy in your password manager so you can sign in yourself. |
   | `IMAP_USER` / `IMAP_PASSWORD` | strongly recommended | Gmail address + [app password](https://myaccount.google.com/apppasswords). Clicks Workday account-verification links, enters Greenhouse verification codes, reads LinkedIn/Indeed alerts, and updates application stages from employer replies. |

6. Push. The workflow starts in **`mode: dry_run`**: it fills forms but doesn't click submit.
   Download the screenshots from each run's artifacts and check them.
   When they look right, set `mode: auto`. Jobs filled during the dry run will then be submitted for real.

## Callback: the tracker app on your phone

`tracker/` is a small web app you can add to your home screen. It has three tabs:
- **To apply** lists the jobs the agent couldn't submit. Each has buttons to open the posting, mark it as applied, or hide it.
- **Applications** lists everything sent, by the agent or by you. Tap one to set its stage (assessment, interview, offer, rejected), add notes, and see what the agent answered on the form.
- **Progress** shows your pipeline, applications per day, and agent controls: a pause switch and a "check now" button.

The app holds no data itself. It reads `state/jobs.json` from this repo using a GitHub token stored only on your phone. Your updates are saved to `state/tracking.json` and `state/control.json`, which the agent never writes, so the two never conflict.

Stages also update on their own: the agent writes what it read from employer emails to `state/inbox.json`, and the app merges it with your changes into one history. Whichever happened last is the current stage, so a rejection email moves a job you marked "Applied" to Rejected, and you can still override it by tapping a stage.

Setup:
1. Repo → Settings → Pages → Source: **GitHub Actions**. Pushing a change under `tracker/` publishes it to `https://<you>.github.io/<repo>/`.
   With a private repo, Pages needs GitHub Pro, which is free with the [Student Developer Pack](https://education.github.com/pack). The published site is just the empty app; your data stays in the private repo.
2. Create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to **only this repo**:
   - **Contents: Read and write**
   - **Actions: Read and write** (needed for the "check now" button and the last-run status)
3. Open the site on your phone. Enter `owner/repo` and the token.
   - iPhone: Share → **Add to Home Screen**.
   - Android: menu → **Install app**.

## Test locally

```sh
py -3.12 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt && python -m playwright install chromium
set ANTHROPIC_API_KEY=...
python -m job_agent discover                       # what would it apply to right now?
python -m job_agent apply <job url> --headed       # watch it fill one form (no submit)
python -m job_agent apply <job url> --submit       # really apply to one job
```

## Limits to know

- **Platform coverage:** Greenhouse, Lever, Ashby, and Workday cover roughly 70% of SimplifyJobs software internships. iCIMS, Oracle, SuccessFactors, and company-built sites (Amazon, Tesla, TikTok...) go to the review queue.
- **Workday:** each company is a separate Workday site with its own account. Many require clicking an emailed verification link before the account works, so set the IMAP secrets. A few tenants add a CAPTCHA or extra steps the agent can't do; those land in the review queue with a screenshot.
- **Timing:** GitHub cron runs every 30 minutes and can lag 5–15 minutes. For near-instant applying, run the same command on a small VPS every few minutes.
- **GitHub Actions minutes:** a private repo gets 2,000 free minutes a month. Every 30 minutes uses about 1,500 of them, because quiet runs skip the browser install. GitHub Pro (free with the Student Developer Pack) raises this to 3,000.
- **Claude cost:** roughly $0.05–0.15 per application with Claude Opus 5.5.
- **Caps:** `max_applications_per_run` / `max_applications_per_day` stop it from mass-applying if a filter is too loose.
