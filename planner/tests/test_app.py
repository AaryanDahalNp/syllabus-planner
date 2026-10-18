from datetime import date
import hashlib
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from planner.forms import CourseForm
from planner.models import AcademicEvent, Course, ExtractionAttempt, GoogleConnection, SyncPreview
from planner.schemas import CandidateEvent, ExtractionResult
from planner.services.calendar_sync import (
    _event_resource,
    apply_sync_preview,
    build_sync_preview,
)
from planner.services.extraction import (
    ExtractionError,
    _gemini_response_schema,
    extract_candidates,
    reserve_extraction_attempt,
    validate_candidates,
)
from planner.services.google_oauth import oauth_flow
from planner.services.ingestion import read_syllabus_pdf


class PlannerWorkflowTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(
            name="CS 101",
            term_start=date(2026, 10, 1),
            term_end=date(2026, 12, 20),
            timezone="America/New_York",
        )

    def test_course_timezone_is_validated(self):
        form = CourseForm(
            data={
                "name": "Bad timezone",
                "timezone": "Not/A_Timezone",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("timezone", form.errors)

    def test_manual_event_requires_reapproval_after_edit(self):
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            approved=True,
        )
        response = self.client.post(
            reverse("event_update", args=[self.course.id, event.id]),
            {
                "title": "Midterm exam",
                "kind": "exam",
                "action": "event",
                "start_date": "2026-10-26",
                "start_time": "",
                "end_date": "",
                "end_time": "",
                "date_text": "October 26, 2026",
            },
        )
        event.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(event.title, "Midterm exam")
        self.assertEqual(event.revision, 2)
        self.assertFalse(event.approved)

    def test_app_requires_explicit_confirmation_before_sync(self):
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            approved=True,
        )
        preview = SyncPreview.objects.create(
            course=self.course,
            fingerprint="fingerprint",
            actions=[{"operation": "add", "event_id": event.id.hex, "revision": event.revision}],
        )
        with patch("planner.views.apply_sync_preview") as apply_preview:
            response = self.client.post(reverse("sync_preview_apply", args=[preview.id]), {})
        self.assertEqual(response.status_code, 302)
        apply_preview.assert_not_called()

    def test_preview_page_lists_changes_and_requires_confirmation(self):
        AcademicEvent.objects.create(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            approved=True,
        )
        action = {
            "operation": "add",
            "event_id": "event-1",
            "revision": 1,
            "google_event_id": "",
            "title": "Midterm",
            "reason": "Add a new event.",
            "resource": {
                "start": {"date": "2026-10-26"},
                "end": {"date": "2026-10-27"},
                "description": "Course: CS 101",
            },
        }
        connection = GoogleConnection.objects.create(token_json="{}", calendar_id="test-calendar")
        with patch(
            "planner.views.build_sync_preview",
            return_value=("fingerprint", [action], ["Overlaps an existing event."], None, connection),
        ):
            response = self.client.post(reverse("sync_preview_create", args=[self.course.id]))
        preview = SyncPreview.objects.get(course=self.course)
        page = self.client.get(reverse("sync_preview_detail", args=[preview.id]))
        self.assertEqual(preview.calendar_id, "test-calendar")
        self.assertEqual(response.status_code, 302)
        self.assertContains(page, "ADD EVENT")
        self.assertContains(page, "Overlaps an existing event.")
        self.assertContains(page, "Target calendar ID:")
        self.assertContains(page, "authorize only the changes listed above")

    def test_unknown_date_cannot_be_approved(self):
        event = AcademicEvent.objects.create(course=self.course, title="TBD quiz")
        response = self.client.post(
            reverse("event_approve", args=[self.course.id, event.id]),
        )
        event.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(event.approved)

    def test_timed_google_event_requires_confirmed_end_time(self):
        event = AcademicEvent(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            start_time="14:00",
            approved=True,
        )
        with self.assertRaises(ValidationError):
            _event_resource(event, self.course)

    def test_multi_day_all_day_event_uses_exclusive_google_end_date(self):
        event = AcademicEvent(
            course=self.course,
            title="Holiday break",
            kind=AcademicEvent.Kind.HOLIDAY,
            start_date=date(2026, 11, 26),
            end_date=date(2026, 11, 27),
            approved=True,
        )
        resource = _event_resource(event, self.course)
        self.assertEqual(resource["start"], {"date": "2026-11-26"})
        self.assertEqual(resource["end"], {"date": "2026-11-28"})

    def test_source_text_is_not_sent_without_explicit_consent(self):
        with patch("planner.views.extract_candidates") as extractor:
            response = self.client.post(
                reverse("extract_syllabus", args=[self.course.id]),
                {"outbound_text": "Midterm: October 26, 2026."},
            )
        self.assertEqual(response.status_code, 302)
        extractor.assert_not_called()
        self.assertFalse(self.course.events.exists())

    @override_settings(AI_PROVIDER="manual", GEMINI_API_KEY="")
    def test_outbound_text_survives_provider_failure(self):
        response = self.client.post(
            reverse("extract_syllabus", args=[self.course.id]),
            {"outbound_text": "Redacted syllabus text.", "consent": "yes"},
        )
        self.course.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.course.syllabus_text, "Redacted syllabus text.")

    def test_changed_source_needs_explicit_review_before_approval(self):
        original_text = "Midterm: October 26, 2026."
        self.course.syllabus_text = original_text
        self.course.save(update_fields=["syllabus_text"])
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            source_excerpt=original_text,
            provenance="gemini",
            source_fingerprint=hashlib.sha256(original_text.encode()).hexdigest(),
        )
        updated_text = "Updated syllabus: Midterm moves to November 2, 2026."
        self.course.syllabus_text = updated_text
        self.course.save(update_fields=["syllabus_text"])
        blocked = self.client.post(reverse("event_approve", args=[self.course.id, event.id]))
        event.refresh_from_db()
        self.assertFalse(event.approved)
        self.assertEqual(blocked.status_code, 302)
        self.client.post(
            reverse("event_approve", args=[self.course.id, event.id]),
            {"acknowledge_issues": "yes"},
        )
        event.refresh_from_db()
        self.assertFalse(event.approved)
        self.client.post(
            reverse("event_update", args=[self.course.id, event.id]),
            {
                "title": "Midterm",
                "kind": "exam",
                "action": "event",
                "start_date": "2026-11-02",
                "start_time": "",
                "end_date": "",
                "end_time": "",
                "date_text": "November 2, 2026",
                "source_excerpt": updated_text,
                "source_page": "1",
            },
        )
        self.client.post(
            reverse("event_approve", args=[self.course.id, event.id]),
            {"acknowledge_issues": "yes"},
        )
        event.refresh_from_db()
        self.assertTrue(event.approved)
        self.assertEqual(
            event.source_fingerprint,
            hashlib.sha256(self.course.syllabus_text.encode()).hexdigest(),
        )

    @override_settings(AI_PROVIDER="manual", GEMINI_API_KEY="")
    def test_manual_ai_mode_fails_explicitly_without_network(self):
        with self.assertRaisesMessage(ExtractionError, "AI extraction is not configured"):
            extract_candidates("Midterm: October 26, 2026.", self.course)

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="test-key")
    def test_provider_failure_logs_safe_diagnostics_without_error_message(self):
        from google import genai

        class ProviderError(Exception):
            code = 400
            message = "Rejected test-key for CS 101: submitted syllabus excerpt."

        provider_error = ProviderError()
        with patch.object(genai, "Client", side_effect=provider_error):
            with self.assertLogs("planner.services.extraction", level="WARNING") as logs:
                with self.assertRaisesMessage(ExtractionError, "AI extraction failed"):
                    extract_candidates("submitted syllabus excerpt.", self.course)
        self.assertIn("ProviderError; status=400", logs.output[0])
        self.assertIn("[redacted]", logs.output[0])
        self.assertNotIn("test-key", logs.output[0])
        self.assertNotIn("CS 101", logs.output[0])
        self.assertNotIn("submitted syllabus excerpt", logs.output[0])

    def test_gemini_schema_has_defined_required_fields_without_refs(self):
        schema = _gemini_response_schema()

        def contains_refs(value):
            if isinstance(value, dict):
                return "$ref" in value or "$defs" in value or any(
                    contains_refs(item) for item in value.values()
                )
            if isinstance(value, list):
                return any(contains_refs(item) for item in value)
            return False

        self.assertFalse(contains_refs(schema))
        event_item_schema = schema["properties"]["events"]["items"]
        self.assertEqual(set(event_item_schema["required"]), set(event_item_schema["properties"]))
        self.assertIn("warnings", schema["properties"])

    @override_settings(AI_MAX_CALLS_PER_DAY=1)
    def test_ai_attempt_limit_is_persisted(self):
        ExtractionAttempt.objects.create(
            course=self.course,
            model="test-model",
            status=ExtractionAttempt.Status.FAILED,
        )
        with self.assertRaisesMessage(ExtractionError, "daily AI request limit"):
            reserve_extraction_attempt(self.course)

    def test_parallel_ai_attempt_is_rejected_while_running(self):
        ExtractionAttempt.objects.create(
            course=self.course,
            model="test-model",
            status=ExtractionAttempt.Status.STARTED,
        )
        with self.assertRaisesMessage(ExtractionError, "already running"):
            reserve_extraction_attempt(self.course)

    def test_candidate_excerpt_and_invalid_date_are_flagged_for_review(self):
        result = ExtractionResult(
            events=[
                CandidateEvent(
                    title="Midterm",
                    kind="exam",
                    action="event",
                    start_date="2026-02-30",
                    start_time=None,
                    end_date=None,
                    end_time=None,
                    date_text="February 30",
                    source_page=1,
                    source_excerpt="Midterm: February 30.",
                    issues=[],
                ),
                CandidateEvent(
                    title="Project",
                    kind="project",
                    action="event",
                    start_date="2026-12-25",
                    start_time=None,
                    end_date=None,
                    end_time=None,
                    date_text="December 25",
                    source_page=1,
                    source_excerpt="Text not in the source.",
                    issues=[],
                ),
                CandidateEvent(
                    title="Quiz",
                    kind="quiz",
                    action="event",
                    start_date="2026-03-04",
                    start_time=None,
                    end_date=None,
                    end_time=None,
                    date_text="03/04/2026",
                    source_page=1,
                    source_excerpt="Quiz date: 03/04/2026.",
                    issues=[],
                ),
            ],
            warnings=[],
        )
        candidates, _ = validate_candidates(
            result,
            "Midterm: February 30.\nProject due December 25.\nQuiz date: 03/04/2026.",
            self.course,
        )
        self.assertIsNone(candidates[0]["start_date"])
        self.assertTrue(any("Invalid date" in issue for issue in candidates[0]["issues"]))
        self.assertTrue(any("could not be verified" in issue for issue in candidates[1]["issues"]))
        self.assertTrue(any("after the course term" in issue for issue in candidates[1]["issues"]))
        self.assertTrue(any("Numeric date is ambiguous" in issue for issue in candidates[2]["issues"]))

    def test_unsearchable_pdf_is_rejected(self):
        uploaded = SimpleUploadedFile("bad.pdf", b"not a PDF", content_type="application/pdf")
        with self.assertRaisesMessage(ValueError, "not a valid PDF"):
            read_syllabus_pdf(uploaded)

    def test_csrf_protects_mutating_forms(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("course_create"),
            {"name": "Other", "timezone": "America/New_York"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Course.objects.count(), 1)

    def test_google_connect_saves_pkce_verifier_in_session(self):
        flow = Mock()
        flow.code_verifier = "v" * 64
        flow.authorization_url.return_value = ("https://accounts.google.com/authorize", "oauth-state")
        with patch("planner.views.oauth_flow", return_value=flow):
            response = self.client.get(reverse("google_connect"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://accounts.google.com/authorize")
        self.assertEqual(self.client.session["google_oauth_state"], "oauth-state")
        self.assertEqual(self.client.session["google_oauth_code_verifier"], "v" * 64)

    def test_google_callback_reuses_pkce_verifier_from_session(self):
        session = self.client.session
        session["google_oauth_state"] = "oauth-state"
        session["google_oauth_code_verifier"] = "v" * 64
        session.save()
        flow = Mock()
        flow.credentials.to_json.return_value = '{"token":"test-token"}'
        with patch("planner.views.oauth_flow", return_value=flow) as make_flow:
            response = self.client.get(
                reverse("google_callback"),
                {"state": "oauth-state", "code": "test-authorization-code"},
            )
        self.assertEqual(response.status_code, 302)
        make_flow.assert_called_once_with(state="oauth-state", code_verifier="v" * 64)
        flow.fetch_token.assert_called_once()
        self.assertTrue(GoogleConnection.objects.filter(token_json='{"token":"test-token"}').exists())
        self.assertNotIn("google_oauth_code_verifier", self.client.session)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="test-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
    )
    def test_oauth_flow_reuses_pkce_verifier_for_callback(self):
        start_flow = oauth_flow()
        _, state = start_flow.authorization_url()
        callback_flow = oauth_flow(state=state, code_verifier=start_flow.code_verifier)
        self.assertEqual(callback_flow.code_verifier, start_flow.code_verifier)
        self.assertFalse(callback_flow.autogenerate_code_verifier)


class CalendarPreviewTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(
            name="CS 101",
            term_start=date(2026, 10, 1),
            term_end=date(2026, 12, 20),
            timezone="America/New_York",
        )
        self.connection = GoogleConnection.objects.create(token_json="{}", calendar_id="primary")

    def test_overlap_warning_does_not_schedule_calendar_write(self):
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            approved=True,
        )
        existing = {
            "id": "other-event",
            "summary": "Office hours",
            "start": {"date": "2026-10-26"},
            "end": {"date": "2026-10-27"},
        }

        class Request:
            def execute(self):
                return {"items": [existing]}

        class Events:
            def list(self, **kwargs):
                return Request()

        class Service:
            def events(self):
                return Events()

        with patch(
            "planner.services.calendar_sync.get_calendar_service",
            return_value=(Service(), self.connection),
        ):
            _, actions, warnings, _, _ = build_sync_preview(self.course)
        self.assertEqual([action["operation"] for action in actions], ["add"])
        self.assertTrue(any("overlaps existing calendar event 'Office hours'" in warning for warning in warnings))

    def test_cancellation_does_not_remove_unmanaged_calendar_event(self):
        AcademicEvent.objects.create(
            course=self.course,
            title="No class",
            kind=AcademicEvent.Kind.CLASS,
            action=AcademicEvent.Action.CANCEL,
            start_date=date(2026, 11, 26),
            approved=True,
        )
        existing = {
            "id": "manually-created",
            "summary": "Lecture",
            "start": {"date": "2026-11-26"},
            "end": {"date": "2026-11-27"},
        }

        class Request:
            def execute(self):
                return {"items": [existing]}

        class Events:
            def list(self, **kwargs):
                return Request()

        class Service:
            def events(self):
                return Events()

        with patch(
            "planner.services.calendar_sync.get_calendar_service",
            return_value=(Service(), self.connection),
        ):
            _, actions, _, _, _ = build_sync_preview(self.course)
        self.assertEqual(actions[0]["operation"], "no_match")
        self.assertEqual(actions[0]["google_event_id"], "")

    def test_approved_cancellation_suppresses_and_removes_its_managed_event(self):
        scheduled = AcademicEvent.objects.create(
            course=self.course,
            title="Lecture",
            kind=AcademicEvent.Kind.CLASS,
            start_date=date(2026, 11, 26),
            approved=True,
            google_event_id="owned-event",
        )
        AcademicEvent.objects.create(
            course=self.course,
            title="No class",
            kind=AcademicEvent.Kind.CLASS,
            action=AcademicEvent.Action.CANCEL,
            start_date=date(2026, 11, 26),
            approved=True,
        )
        existing = {
            "id": "owned-event",
            "etag": "version-1",
            "summary": "Lecture",
            "start": {"date": "2026-11-26"},
            "end": {"date": "2026-11-27"},
            "extendedProperties": {
                "private": {
                    "syllabusPlannerEvent": str(scheduled.id),
                    "syllabusPlannerCourse": str(self.course.id),
                    "syllabusPlannerKind": "class",
                }
            },
        }

        class Request:
            def execute(self):
                return {"items": [existing]}

        class Events:
            def list(self, **kwargs):
                return Request()

        class Service:
            def events(self):
                return Events()

        with patch(
            "planner.services.calendar_sync.get_calendar_service",
            return_value=(Service(), self.connection),
        ):
            _, actions, _, _, _ = build_sync_preview(self.course)
        self.assertEqual(
            {action["operation"] for action in actions},
            {"remove", "suppressed"},
        )
        self.assertEqual(
            next(action for action in actions if action["operation"] == "remove")["google_event_id"],
            "owned-event",
        )

    def test_stale_preview_refuses_to_write(self):
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Midterm",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            approved=True,
        )
        preview = SyncPreview.objects.create(
            course=self.course,
            fingerprint="old",
            actions=[{"event_id": event.id.hex, "revision": event.revision}],
        )
        service = type("Service", (), {"events": lambda self: self})()
        with patch(
            "planner.services.calendar_sync.build_sync_preview",
            return_value=("new", [], [], service, self.connection),
        ):
            from planner.services.calendar_sync import CalendarSyncError, apply_sync_preview

            with self.assertRaisesMessage(CalendarSyncError, "Google Calendar changed after preview"):
                apply_sync_preview(preview)
        preview.refresh_from_db()
        self.assertEqual(preview.status, SyncPreview.Status.STALE)

    def test_confirmed_update_uses_google_etag(self):
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Midterm retake",
            kind=AcademicEvent.Kind.EXAM,
            start_date=date(2026, 10, 26),
            approved=True,
            revision=2,
            google_event_id="managed-event",
        )
        existing = {
            "id": "managed-event",
            "etag": "version-1",
            "summary": "Midterm",
            "start": {"date": "2026-10-26"},
            "end": {"date": "2026-10-27"},
            "extendedProperties": {
                "private": {
                    "syllabusPlannerEvent": str(event.id),
                    "syllabusPlannerCourse": str(self.course.id),
                    "syllabusPlannerKind": "exam",
                }
            },
        }
        calls = {}

        class Request:
            def __init__(self, result=None):
                self.result = result
                self.headers = {}

            def execute(self):
                calls["headers"] = self.headers.copy()
                return self.result

        class Events:
            def list(self, **kwargs):
                return Request({"items": [existing]})

            def patch(self, **kwargs):
                calls.update(kwargs)
                return Request({"etag": "version-2"})

        class Service:
            def events(self):
                return Events()

        service = Service()
        with patch(
            "planner.services.calendar_sync.get_calendar_service",
            return_value=(service, self.connection),
        ):
            fingerprint, actions, warnings, _, _ = build_sync_preview(self.course)
            preview = SyncPreview.objects.create(
                course=self.course,
                fingerprint=fingerprint,
                actions=actions,
                warnings=warnings,
            )
            applied, _ = apply_sync_preview(preview)
        event.refresh_from_db()
        self.assertEqual(applied[0]["operation"], "update")
        self.assertEqual(calls["headers"], {"If-Match": "version-1"})
        self.assertEqual(calls["body"]["summary"], "Midterm retake")
        self.assertEqual(event.google_etag, "version-2")

    def test_confirmed_removal_uses_google_etag(self):
        event = AcademicEvent.objects.create(
            course=self.course,
            title="Lecture",
            kind=AcademicEvent.Kind.CLASS,
            start_date=date(2026, 11, 26),
            approved=True,
            google_event_id="managed-event",
        )
        cancellation = AcademicEvent.objects.create(
            course=self.course,
            title="No class",
            kind=AcademicEvent.Kind.CLASS,
            action=AcademicEvent.Action.CANCEL,
            start_date=date(2026, 11, 26),
            approved=True,
        )
        existing = {
            "id": "managed-event",
            "etag": "version-1",
            "summary": "Lecture",
            "start": {"date": "2026-11-26"},
            "end": {"date": "2026-11-27"},
            "extendedProperties": {
                "private": {
                    "syllabusPlannerEvent": str(event.id),
                    "syllabusPlannerCourse": str(self.course.id),
                    "syllabusPlannerKind": "class",
                }
            },
        }
        calls = {}

        class Request:
            headers = {}

            def execute(self):
                calls["headers"] = self.headers.copy()
                return {}

        class Events:
            def list(self, **kwargs):
                return type("ListRequest", (), {"execute": lambda self: {"items": [existing]}})()

            def delete(self, **kwargs):
                calls.update(kwargs)
                return Request()

        class Service:
            def events(self):
                return Events()

        with patch(
            "planner.services.calendar_sync.get_calendar_service",
            return_value=(Service(), self.connection),
        ):
            fingerprint, actions, warnings, _, _ = build_sync_preview(self.course)
            preview = SyncPreview.objects.create(
                course=self.course,
                fingerprint=fingerprint,
                actions=actions,
                warnings=warnings,
            )
            applied, _ = apply_sync_preview(preview)
        event.refresh_from_db()
        cancellation.refresh_from_db()
        self.assertEqual({action["operation"] for action in applied}, {"remove", "suppressed"})
        self.assertEqual(calls["headers"], {"If-Match": "version-1"})
        self.assertEqual(calls["calendarId"], "primary")
        self.assertEqual(calls["eventId"], "managed-event")
        self.assertEqual(event.google_event_id, "")
