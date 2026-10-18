import logging
import hashlib

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from planner.forms import AcademicEventForm, CourseForm, GoogleCalendarForm, SyllabusForm
from planner.models import AcademicEvent, Course, GoogleConnection, SyncPreview
from planner.services.calendar_sync import CalendarSyncError, apply_sync_preview, build_sync_preview
from planner.services.extraction import (
    ExtractionError,
    extract_candidates,
    finish_extraction_attempt,
    reserve_extraction_attempt,
)
from planner.services.google_oauth import oauth_flow
from planner.services.ingestion import read_syllabus_pdf

logger = logging.getLogger(__name__)


def dashboard(request):
    return render(
        request,
        "planner/dashboard.html",
        {"courses": Course.objects.all(), "connection": GoogleConnection.objects.first()},
    )


def course_create(request):
    form = CourseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        course = form.save()
        messages.success(request, "Course created. Paste a syllabus or add an event manually.")
        return redirect("course_detail", course_id=course.id)
    return render(request, "planner/course_form.html", {"form": form})


def course_detail(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    event_rows = [
        {"event": event, "form": AcademicEventForm(instance=event)}
        for event in course.events.all()
    ]
    return render(
        request,
        "planner/course_detail.html",
        {
            "course": course,
            "event_rows": event_rows,
            "syllabus_form": SyllabusForm(),
            "event_form": AcademicEventForm(),
            "connection": GoogleConnection.objects.first(),
            "ai_ready": settings.AI_PROVIDER == "gemini" and bool(settings.GEMINI_API_KEY),
            "has_managed_events": course.events.filter(google_event_id__gt="").exists(),
        },
    )


@require_POST
def save_syllabus(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    form = SyllabusForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("course_detail", course_id=course.id)
    uploaded = form.cleaned_data.get("pdf")
    try:
        if uploaded:
            course.syllabus_text = read_syllabus_pdf(uploaded)
            course.source_filename = uploaded.name[:255]
        else:
            course.syllabus_text = form.cleaned_data["text"].strip()
            course.source_filename = ""
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("course_detail", course_id=course.id)
    course.save(update_fields=["syllabus_text", "source_filename"])
    messages.success(request, "Syllabus saved locally. Review the exact text before requesting AI extraction.")
    return redirect("course_detail", course_id=course.id)


@require_POST
def extract_syllabus(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    if request.POST.get("consent") != "yes":
        messages.error(request, "Confirm that you reviewed the syllabus text before sending it to the AI provider.")
        return redirect("course_detail", course_id=course.id)
    text = request.POST.get("outbound_text", "").strip()
    if not text or len(text) > 60000:
        messages.error(request, "Review and provide between 1 and 60,000 characters of syllabus text.")
        return redirect("course_detail", course_id=course.id)
    if text != course.syllabus_text:
        course.syllabus_text = text
        course.source_filename = ""
        course.save(update_fields=["syllabus_text", "source_filename"])
    attempt = None
    try:
        if settings.AI_PROVIDER == "gemini" and settings.GEMINI_API_KEY:
            attempt = reserve_extraction_attempt(course)
        candidates, warnings = extract_candidates(text, course)
    except ExtractionError as exc:
        if attempt:
            finish_extraction_attempt(attempt, succeeded=False)
        logger.warning("Syllabus extraction failed for course %s: %s", course.id, exc)
        messages.error(request, str(exc))
        return redirect("course_detail", course_id=course.id)
    if attempt:
        finish_extraction_attempt(attempt, succeeded=True)
    created_count = 0
    rejected_count = 0
    for candidate in candidates:
        try:
            event = AcademicEvent(
                course=course,
                provenance="gemini",
                source_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                **candidate,
            )
            event.full_clean()
            event.save()
            created_count += 1
        except (ValidationError, TypeError, ValueError):
            rejected_count += 1
            logger.warning("Rejected invalid candidate for course %s", course.id)
    for warning in warnings:
        messages.warning(request, warning)
    messages.success(request, f"Created {created_count} draft suggestion(s). None were approved or synced.")
    if rejected_count:
        messages.warning(
            request,
            f"{rejected_count} suggestion(s) could not be saved because their fields were invalid. "
            "Check the syllabus and add them manually if needed.",
        )
    return redirect("course_detail", course_id=course.id)


@require_POST
def event_create(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    form = AcademicEventForm(request.POST)
    if form.is_valid():
        event = form.save(commit=False)
        event.course = course
        event.provenance = "manual"
        try:
            event.full_clean()
            event.save()
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "Draft event added. Review and approve it before calendar sync.")
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect("course_detail", course_id=course.id)


@require_POST
def event_update(request, course_id, event_id):
    event = get_object_or_404(AcademicEvent, id=event_id, course_id=course_id)
    form = AcademicEventForm(request.POST, instance=event)
    if form.is_valid():
        updated = form.save(commit=False)
        updated.revision += 1
        updated.approved = False
        updated.issues = []
        updated.full_clean()
        updated.save()
        messages.success(request, "Event updated. Approval was reset; review it again before syncing.")
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect("course_detail", course_id=course_id)


@require_POST
def event_approve(request, course_id, event_id):
    event = get_object_or_404(AcademicEvent, id=event_id, course_id=course_id)
    try:
        event.full_clean()
    except ValidationError as exc:
        messages.error(request, "Resolve the event's date or time fields before approval: " + "; ".join(exc.messages))
        return redirect("course_detail", course_id=course_id)
    if not event.start_date:
        messages.error(request, "A confirmed date is required before approval.")
    elif (
        event.action == AcademicEvent.Action.EVENT
        and event.start_time
        and not event.end_time
    ):
        messages.error(request, "Add a confirmed end time before approving this timed calendar event.")
    elif (event.issues or event.source_changed) and request.POST.get("acknowledge_issues") != "yes":
        messages.error(request, "Review the source warnings and explicitly confirm them before approval.")
    elif (
        event.source_changed
        and " ".join(event.source_excerpt.split())
        not in " ".join(event.course.syllabus_text.split())
    ):
        messages.error(request, "Update the source excerpt to match the current syllabus before approval.")
    else:
        event.approved = True
        if event.source_changed:
            event.source_fingerprint = hashlib.sha256(
                event.course.syllabus_text.encode("utf-8")
            ).hexdigest()
        event.issues = []
        event.save(update_fields=["approved", "issues", "source_fingerprint", "updated_at"])
        messages.success(request, "Event approved.")
    return redirect("course_detail", course_id=course_id)


@require_POST
def event_delete(request, course_id, event_id):
    event = get_object_or_404(AcademicEvent, id=event_id, course_id=course_id)
    if event.google_event_id:
        messages.error(request, "This event is already in Google Calendar. Use a syllabus cancellation to propose removal.")
    else:
        event.delete()
        messages.success(request, "Event deleted locally.")
    return redirect("course_detail", course_id=course_id)


def google_settings(request):
    connection = GoogleConnection.objects.first()
    form = GoogleCalendarForm(request.POST or None, initial={"calendar_id": connection.calendar_id if connection else "primary"})
    if request.method == "POST" and form.is_valid():
        if connection:
            connection.calendar_id = form.cleaned_data["calendar_id"]
            connection.save(update_fields=["calendar_id"])
        else:
            messages.error(request, "Connect Google Calendar before choosing a calendar.")
        if connection:
            messages.success(request, "Calendar selection saved.")
        return redirect("google_settings")
    context = {
        "connection": connection,
        "form": form,
        "oauth_configured": bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET),
    }
    return render(request, "planner/google_settings.html", context)


def google_connect(request):
    try:
        flow = oauth_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
    except ImproperlyConfigured as exc:
        messages.error(request, str(exc))
        return redirect("google_settings")
    request.session["google_oauth_state"] = state
    request.session["google_oauth_code_verifier"] = flow.code_verifier
    return redirect(authorization_url)


def google_callback(request):
    state = request.session.pop("google_oauth_state", None)
    code_verifier = request.session.pop("google_oauth_code_verifier", None)
    if not state or not code_verifier or request.GET.get("state") != state:
        messages.error(request, "Google authorization state did not match. Please try connecting again.")
        return redirect("google_settings")
    if request.GET.get("error"):
        messages.error(request, "Google Calendar authorization was cancelled or denied.")
        return redirect("google_settings")
    try:
        flow = oauth_flow(state=state, code_verifier=code_verifier)
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        token_json = flow.credentials.to_json()
    except Exception as exc:
        logger.warning("Google OAuth callback failed: %s", type(exc).__name__)
        messages.error(request, "Google authorization failed. Check OAuth setup and retry.")
        return redirect("google_settings")
    GoogleConnection.objects.update_or_create(
        pk=1,
        defaults={"token_json": token_json, "calendar_id": "primary"},
    )
    messages.success(request, "Google Calendar connected. No calendar events were changed.")
    return redirect("google_settings")


@require_POST
def google_disconnect(request):
    GoogleConnection.objects.all().delete()
    messages.success(request, "Google Calendar disconnected. Existing calendar events were not changed.")
    return redirect("google_settings")


@require_POST
def sync_preview_create(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    try:
        fingerprint, actions, warnings, _, connection = build_sync_preview(course)
    except (CalendarSyncError, ValidationError) as exc:
        messages.error(request, str(exc))
        return redirect("course_detail", course_id=course.id)
    preview = SyncPreview.objects.create(
        course=course,
        calendar_id=connection.calendar_id,
        fingerprint=fingerprint,
        actions=actions,
        warnings=warnings,
    )
    return redirect("sync_preview_detail", preview_id=preview.id)


def sync_preview_detail(request, preview_id):
    preview = get_object_or_404(SyncPreview, id=preview_id)
    return render(request, "planner/sync_preview.html", {"preview": preview})


@require_POST
def sync_preview_apply(request, preview_id):
    preview = get_object_or_404(SyncPreview, id=preview_id)
    if request.POST.get("confirm_changes") != "yes":
        messages.error(request, "Confirm that you reviewed the complete change list before applying it.")
        return redirect("sync_preview_detail", preview_id=preview.id)
    try:
        actions, _ = apply_sync_preview(preview)
    except (CalendarSyncError, ValidationError) as exc:
        messages.error(request, str(exc))
        return redirect("sync_preview_detail", preview_id=preview.id)
    changed = sum(action["operation"] in ("add", "update", "remove") for action in actions)
    messages.success(request, f"Google Calendar sync complete: {changed} change(s) applied.")
    return redirect("course_detail", course_id=preview.course_id)


def privacy_delete_course(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    if request.method == "POST":
        if request.POST.get("confirm") != course.name:
            messages.error(request, "Type the course name exactly to confirm local data deletion.")
            return redirect("course_detail", course_id=course.id)
        if course.events.filter(google_event_id__gt="").exists():
            messages.error(
                request,
                "This course has app-owned Google Calendar events. Add dated cancellation rows and preview their removal before deleting local records.",
            )
            return redirect("course_detail", course_id=course.id)
        course.delete()
        messages.success(request, "Course, syllabus text, drafts, and sync previews deleted locally.")
        return redirect("dashboard")
    return render(
        request,
        "planner/delete_course.html",
        {
            "course": course,
            "has_managed_events": course.events.filter(google_event_id__gt="").exists(),
        },
    )
