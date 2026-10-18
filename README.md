# Syllabus Planner

A local-first syllabus-to-calendar planner. It turns pasted or searchable-PDF syllabus text into **unapproved event drafts**, lets you verify dates against source excerpts, then prepares a Google Calendar change list for your explicit confirmation.

> **Current scope:** Local Django MVP; paste/PDF input; optional Gemini extraction; manual event entry; editable, source-backed review; Google Calendar OAuth; overlap warnings; add/update/remove preview; and confirmed sync for events owned by this app. No live Google or Gemini credentials are included. Google OAuth account connection has been verified during development, but end-to-end calendar event writes and successful Gemini extraction have not been verified.

## Why this project

Syllabus dates are easy to misread, and a calendar sync should not silently rewrite a student's schedule. This app puts the person in the loop twice: first to approve extracted dates, then to approve the exact calendar changes.

## Features

- Create courses with term dates, an IANA timezone, and an explicit numeric date convention.
- Paste syllabus text or upload a searchable PDF (5 MiB, 30 pages, and 60,000 extracted characters maximum).
- Inspect and edit the exact text sent to Gemini; a separate checkbox is required for every extraction request.
- Keep edited outbound text locally even if a provider request fails, and flag AI evidence that no longer matches the saved syllabus until you explicitly review it.
- Receive candidate exams, assignments, quizzes, projects, classes, holidays, and cancellation signals such as “no class”.
- Review each suggestion's raw wording, source excerpt, page, date/time, and validation issues. AI suggestions are never approved automatically.
- Flag ambiguous numeric dates and dates outside the stated course term for human resolution.
- Create and edit events manually when AI is unavailable or wrong.
- Connect a Google account and select a calendar.
- Review additions, updates to app-owned events, proposed removals of app-owned events, no-match cancellation notes, and detected overlaps **before** writing to Google Calendar.
- Require a separate confirmation checkbox for the exact preview. A changed calendar or event makes that preview stale.
- Keep other calendar events read-only. A cancellation can only remove app-owned events with the same course, event category, and date.
- Store course and event records locally in SQLite.

## How calendar safety works

1. A candidate stays a draft until you approve it. Editing an approved event resets its approval.
2. A timed event needs a confirmed start and end time; the app does not guess its duration. A date-only event stays all-day.
3. A sync preview reads calendar events to find overlaps and app-managed records. It does not write anything.
4. The preview lists each add, update, and removal separately, including the current and proposed values when applicable.
5. On confirmation, the app reads the calendar again and checks that the snapshot and event revisions still match. If they do not, it refuses the stale preview.
6. Google writes are limited to events created by this app (identified by private event metadata). Calendar events without that metadata are never updated or deleted. They can still appear as overlap warnings.
7. “No class” / “no assignment” is a cancellation *proposal*, not a bulk-delete command. Only same-date, same-category events previously created by this app can be proposed for removal.

Google Calendar writes are immediate and cannot be rolled back by this application. If Google reports a failure after earlier operations succeeded, the sync is marked partially failed; inspect the calendar before retrying.

## Stack and data flow

- Python 3.12 recommended, Django 5.2, SQLite, server-rendered templates
- Pydantic validation and the Google Gen AI SDK (optional; no calls in manual mode)
- Google OAuth + Google Calendar API (only after account connection and explicit sync confirmation)
- pypdf for selectable-text PDF extraction

```text
Paste/PDF → local text preview → explicit AI consent → candidate events
                                                    ↓
                                         source/date validation
                                                    ↓
                                    human correction and approval
                                                    ↓
Google Calendar ← explicit confirmation ← change preview + overlap scan
```

No public accounts, hosting, recurring-class extraction, study scheduler, notifications, or background sync are included.

## Run locally

Use Python 3.12 and create a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

`requirements.txt` pins the versions used for the MVP checks; `requirements.in` records the direct dependency ranges.

Set `DJANGO_SECRET_KEY` in `.env` to a locally generated value. One way:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then initialize and start the local app:

```bash
python manage.py migrate
python manage.py check
python manage.py test
python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>. The server binds to loopback. Do not expose Django's development server to the internet.

### Synthetic demo

To seed clearly labeled sample data:

```bash
python manage.py seed_demo
```

The sample course is synthetic and includes two dated items and a “no class” signal. Seeding does not call AI or Google and does not change a calendar. The seed's events are already approved for demonstrating the preview flow.

## Optional Gemini extraction

Manual entry works without an API key. To enable extraction:

1. Create an eligible Gemini API key in Google AI Studio and check the model's current availability, region terms, and quota.
2. Keep billing disabled if you intend to stay on an eligible free tier; quotas and eligibility can change.
3. In `.env`, set:

```dotenv
AI_PROVIDER=gemini
GEMINI_API_KEY=your-local-key
GEMINI_MODEL=gemini-3.5-flash-lite
```

The model name is configurable; use one currently available to your account. No silent provider fallback is used. Syllabus content is sent only after you inspect the outbound text and explicitly consent. Remove personal, identifying, and confidential information first. Treat model output as a suggestion: a source excerpt supports traceability, not date correctness.

The local database counts attempted requests and enforces `AI_MAX_CALLS_PER_DAY` (default 20); it also rejects a second request while one is already running. This is a local guardrail, not a billing guarantee. Automated tests do not make live AI requests. This project has not measured extraction accuracy or verified a live model call.

## Optional Google Calendar connection

Calendar integration requires OAuth credentials you configure; none are included in the repository.

1. In Google Cloud Console, create or select a project and enable the **Google Calendar API**.
2. Configure the OAuth consent screen and add your Google account as a test user if the app is in testing.
3. Create an OAuth client for a **Web application**. Add this exact authorized redirect URI:

   ```text
   http://127.0.0.1:8000/google/callback/
   ```

4. Add the OAuth client values to your local `.env`:

   ```dotenv
   GOOGLE_OAUTH_CLIENT_ID=your-client-id
   GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
   OAUTHLIB_INSECURE_TRANSPORT=1
   ```

   `OAUTHLIB_INSECURE_TRANSPORT=1` is needed because this loopback-only development setup uses HTTP. It disables OAuthlib's HTTPS transport check for the app process; use it only on your own computer with the development server bound to `127.0.0.1`. Never use this setting for a public or deployed app.

5. Restart the development server. Open **Google Calendar** in the app and connect the account. Choose `primary` or enter another calendar's ID.
6. In a course, approve the events, choose **Review calendar changes**, inspect the complete list and all overlap warnings, then check the authorization box to apply it.

The app requests the `calendar.events` scope to read and manage events. Google OAuth tokens are stored in the local SQLite database. That database and `.env` are ignored by Git; do not publish them or share the local server. Disconnecting removes the local token but does not remove calendar events. Deleting a course also does not remove events from Google.

## Privacy and limitations

- This is a single-user development app without login. It is intended to run only on your computer.
- SQLite contains course text and OAuth tokens in local storage. Use a private machine and do not commit the database.
- PDF extraction supports text-based PDFs only; scanned documents need OCR, which is not included.
- The AI can miss events, misunderstand wording, or return a wrong date. Human review is required.
- Numeric-date ambiguity, unknown dates, out-of-term dates, invalid dates, and missing source evidence are surfaced for review. A page excerpt is not proof that an interpretation is correct.
- Existing calendar events that the app did not create are never changed or removed, even if a syllabus cancellation seems to match them. They may be flagged as overlaps.
- A preview can become stale; refresh it instead of reusing it.
- No ICS fallback, recurring meeting support, multi-user isolation, remote deployment, scheduler, or accuracy claim is provided.

## Tests

Tests run locally with Django's test runner and use mocked calendar/AI boundaries. They do not need credentials and do not contact external APIs.

```bash
python manage.py test
python manage.py check
python -m pip check
```

## GitHub release checklist

Before publishing:

- Verify `.env` and `db.sqlite3` are not staged.
- Do not include real syllabi, personal calendars, OAuth tokens, API keys, or private screenshots.
- Add screenshots only after creating them with synthetic data.
- Configure credentials locally after cloning; never paste them into source files.
- Run the setup and tests from a clean checkout.

This folder has not been initialized as a Git repository and no remote was added. After reviewing the files, you can publish it with:

```bash
git init -b main
git add .
git status --short
git diff --cached --stat
git commit -m "Build syllabus planner MVP"
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
git push -u origin main
```

Replace the remote URL with your own empty GitHub repository. Confirm that `.env`, `db.sqlite3`, local Python environments, and `Syllabus_Planner_MVP_Build_Guide.md` are not staged. The `.gitignore` already excludes them; `.env.example` is safe to publish.
