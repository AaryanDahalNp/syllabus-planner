import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from planner.models import AcademicEvent, GoogleConnection

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
PRIVATE_EVENT_KEY = "syllabusPlannerEvent"
PRIVATE_COURSE_KEY = "syllabusPlannerCourse"
PRIVATE_KIND_KEY = "syllabusPlannerKind"


class CalendarSyncError(Exception):
    pass


def get_calendar_service():
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise CalendarSyncError(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env before connecting Google Calendar."
        )
    connection = GoogleConnection.objects.first()
    if not connection:
        raise CalendarSyncError("Connect a Google account before creating a sync preview.")
    try:
        credentials = Credentials.from_authorized_user_info(json.loads(connection.token_json), SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            connection.token_json = credentials.to_json()
            connection.save(update_fields=["token_json", "connected_at"])
        if not credentials.valid:
            raise CalendarSyncError("Google authorization expired. Reconnect your account.")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False), connection
    except CalendarSyncError:
        raise
    except Exception as exc:
        raise CalendarSyncError("Google authorization is invalid. Reconnect your account.") from exc


def _date_bounds(event):
    if not event.start_date:
        raise ValidationError(f"{event.title} has no confirmed date.")
    end_date = event.end_date or event.start_date
    return event.start_date, end_date


def _as_local_datetime(day, clock, zone):
    return datetime.combine(day, clock or time.min, tzinfo=zone)


def _event_resource(event, course):
    if event.action != AcademicEvent.Action.EVENT:
        raise ValidationError("Cancellation rows are not calendar events.")
    if not event.approved or event.blocking_issues:
        raise ValidationError(f"Review and approve '{event.title}' before syncing.")
    zone = ZoneInfo(course.timezone)
    start_date, end_date = _date_bounds(event)
    resource = {
        "summary": event.title,
        "description": (
            f"Course: {course.name}\n"
            f"Type: {event.get_kind_display()}\n"
            f"Source: {event.source_excerpt or event.date_text or 'User-entered'}"
        ),
        "extendedProperties": {
            "private": {
                PRIVATE_EVENT_KEY: str(event.id),
                PRIVATE_COURSE_KEY: str(course.id),
                PRIVATE_KIND_KEY: event.kind,
            }
        },
    }
    if event.start_time:
        if not event.end_time:
            raise ValidationError(f"Add an end time for '{event.title}' before syncing it to Google Calendar.")
        end_day = event.end_date or event.start_date
        start = _as_local_datetime(start_date, event.start_time, zone)
        end = _as_local_datetime(end_day, event.end_time, zone)
        if end <= start:
            raise ValidationError(f"End must be after start for '{event.title}'.")
        resource["start"] = {"dateTime": start.isoformat(), "timeZone": course.timezone}
        resource["end"] = {"dateTime": end.isoformat(), "timeZone": course.timezone}
    else:
        resource["start"] = {"date": start_date.isoformat()}
        resource["end"] = {"date": (end_date + timedelta(days=1)).isoformat()}
    return resource


def _time_bounds(events, zone):
    dates = [event.start_date for event in events if event.start_date]
    if not dates:
        raise ValidationError("Add at least one dated, approved event before syncing.")
    start = datetime.combine(min(dates) - timedelta(days=1), time.min, tzinfo=zone)
    end = datetime.combine(max(dates) + timedelta(days=2), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def _fetch_calendar_events(service, connection, events):
    zone = ZoneInfo(events[0].course.timezone)
    time_min, time_max = _time_bounds(events, zone)
    found = []
    token = None
    while True:
        response = service.events().list(
            calendarId=connection.calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            pageToken=token,
            maxResults=2500,
        ).execute()
        found.extend(response.get("items", []))
        token = response.get("nextPageToken")
        if not token:
            break
    by_id = {item["id"]: item for item in found if item.get("id")}
    for event in events:
        if event.google_event_id and event.google_event_id not in by_id:
            try:
                item = service.events().get(
                    calendarId=connection.calendar_id, eventId=event.google_event_id
                ).execute()
                by_id[item["id"]] = item
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
    return list(by_id.values())


def _start_end(resource, zone):
    start = resource.get("start", {})
    end = resource.get("end", {})
    if start.get("date"):
        start_dt = datetime.combine(date.fromisoformat(start["date"]), time.min, tzinfo=zone)
        end_dt = datetime.combine(date.fromisoformat(end["date"]), time.min, tzinfo=zone)
    else:
        start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(zone)
        end_dt = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone(zone)
    return start_dt, end_dt


def _event_interval(event, course):
    start, end = _start_end(event, ZoneInfo(course.timezone))
    return start, end


def _display_interval(resource, course):
    start, end = _event_interval(resource, course)
    if resource.get("start", {}).get("date"):
        last_date = end.date() - timedelta(days=1)
        if last_date == start.date():
            return f"all day {start.strftime('%b %d, %Y')}"
        return f"all day {start.strftime('%b %d, %Y')} – {last_date.strftime('%b %d, %Y')}"
    return f"{start.strftime('%b %d, %Y %I:%M %p')} – {end.strftime('%b %d, %Y %I:%M %p')}"


def _overlap_warnings(existing, actions, course):
    warnings = []
    proposed = [action for action in actions if action["operation"] in ("add", "update")]
    deleted_ids = {action["google_event_id"] for action in actions if action["operation"] == "remove"}
    ignored_ids = {
        action["google_event_id"]
        for action in proposed
        if action["google_event_id"]
    } | deleted_ids
    for action in proposed:
        start, end = _event_interval(action["resource"], course)
        for current in existing:
            if current.get("id") in ignored_ids:
                continue
            current_start, current_end = _event_interval(current, course)
            if start < current_end and current_start < end:
                warnings.append(
                    f"'{action['title']}' overlaps existing calendar event "
                    f"'{current.get('summary', 'Untitled event')}' ({_display_interval(current, course)})."
                )
    for index, action in enumerate(proposed):
        start, end = _event_interval(action["resource"], course)
        for other in proposed[index + 1 :]:
            other_start, other_end = _event_interval(other["resource"], course)
            if start < other_end and other_start < end:
                warnings.append(
                    f"'{action['title']}' overlaps another proposed event, '{other['title']}' "
                    f"({_display_interval(other['resource'], course)})."
                )
    return warnings


def build_sync_preview(course):
    if not course.events.filter(approved=True).exists():
        raise ValidationError("Approve at least one event before syncing.")
    events = list(course.events.filter(approved=True).order_by("start_date", "id"))
    linked_events = list(course.events.filter(google_event_id__gt=""))
    scan_events = list({event.id: event for event in events + linked_events}.values())
    service, connection = get_calendar_service()
    try:
        existing = _fetch_calendar_events(service, connection, scan_events)
    except HttpError as exc:
        if exc.resp.status == 404:
            message = "Google Calendar could not find the selected calendar ID. Check Calendar settings."
        elif exc.resp.status in (401, 403):
            message = "Google denied access to this calendar. Reconnect or choose a calendar you can manage."
        elif exc.resp.status == 429:
            message = "Google Calendar rate-limited this request. Wait briefly, then create a fresh preview."
        else:
            message = "Google Calendar could not be read. Retry after checking the connection."
        raise CalendarSyncError(message) from exc
    except Exception as exc:
        raise CalendarSyncError(
            "Google Calendar could not be reached. Check the connection and try again."
        ) from exc
    by_id = {item.get("id"): item for item in existing}
    linked_by_google_id = {event.google_event_id: event for event in linked_events}
    by_managed_id = {}
    for current in existing:
        private = current.get("extendedProperties", {}).get("private", {})
        managed_id = private.get(PRIVATE_EVENT_KEY)
        if managed_id:
            if managed_id in by_managed_id:
                raise ValidationError(
                    "Google Calendar contains duplicate app-owned copies of an event. "
                    "Resolve the duplicates in Google Calendar before syncing."
                )
            by_managed_id[managed_id] = current
    actions = []
    cancellation_targets = {
        (event.kind, event.start_date)
        for event in events
        if event.action == AcademicEvent.Action.CANCEL
    }
    for event in events:
        if event.action == AcademicEvent.Action.CANCEL:
            matching = []
            for current in existing:
                private = current.get("extendedProperties", {}).get("private", {})
                linked = linked_by_google_id.get(current.get("id"))
                linked_match = (
                    linked
                    and private.get(PRIVATE_EVENT_KEY) == str(linked.id)
                    and linked.kind == event.kind
                    and linked.start_date == event.start_date
                )
                if (
                    private.get(PRIVATE_COURSE_KEY) == str(course.id)
                    and (
                        linked_match
                        or (
                            private.get(PRIVATE_KIND_KEY) == event.kind
                            and _start_end(current, ZoneInfo(course.timezone))[0].date() == event.start_date
                        )
                    )
                ):
                    matching.append(current)
            if matching:
                for current in matching:
                    actions.append(
                        {
                            "operation": "remove",
                            "event_id": event.id.hex,
                            "revision": event.revision,
                            "google_event_id": current["id"],
                            "etag": current.get("etag", ""),
                            "title": current.get("summary", event.title),
                            "previous": {
                                "start": current.get("start"),
                                "end": current.get("end"),
                            },
                            "reason": f"Cancellation indicated by syllabus: {event.title}",
                        }
                    )
            else:
                actions.append(
                    {
                        "operation": "no_match",
                        "event_id": event.id.hex,
                        "revision": event.revision,
                        "google_event_id": "",
                        "title": event.title,
                        "reason": "No matching app-owned event exists on this date; other calendar events are untouched.",
                    }
                )
            continue
        if (event.kind, event.start_date) in cancellation_targets:
            actions.append(
                {
                    "operation": "suppressed",
                    "event_id": event.id.hex,
                    "revision": event.revision,
                    "google_event_id": event.google_event_id,
                    "title": event.title,
                    "reason": "A same-date cancellation is approved; this event will not be added or updated.",
                }
            )
            continue
        resource = _event_resource(event, course)
        current = (
            by_id.get(event.google_event_id) if event.google_event_id else None
        ) or by_managed_id.get(str(event.id))
        if current:
            private = current.get("extendedProperties", {}).get("private", {})
            if (
                private.get(PRIVATE_EVENT_KEY) != str(event.id)
                or private.get(PRIVATE_COURSE_KEY) != str(course.id)
            ):
                raise ValidationError(
                    f"Stored Google event for '{event.title}' is not marked as app-owned. "
                    "It will not be changed."
                )
        operation = "update" if current else "add"
        actions.append(
            {
                "operation": operation,
                "event_id": event.id.hex,
                "revision": event.revision,
                "google_event_id": current.get("id", "") if current else "",
                "etag": current.get("etag", "") if current else "",
                "title": event.title,
                "reason": "Update the event created by this app." if current else "Add a new event.",
                "resource": resource,
                "previous": (
                    {
                        "summary": current.get("summary"),
                        "start": current.get("start"),
                        "end": current.get("end"),
                        "etag": current.get("etag", ""),
                    }
                    if current
                    else None
                ),
            }
        )
    warnings = _overlap_warnings(existing, actions, course)
    snapshot = [
        {
            "id": item.get("id"),
            "etag": item.get("etag"),
            "summary": item.get("summary"),
            "start": item.get("start"),
            "end": item.get("end"),
            "private": item.get("extendedProperties", {}).get("private", {}),
        }
        for item in sorted(existing, key=lambda row: row.get("id", ""))
    ]
    payload = {"calendar": connection.calendar_id, "snapshot": snapshot, "actions": actions}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return digest, actions, warnings, service, connection


def apply_sync_preview(preview):
    if preview.status != preview.Status.PENDING:
        raise CalendarSyncError("This preview is no longer awaiting confirmation. Create a new preview.")
    digest, actions, warnings, service, connection = build_sync_preview(preview.course)
    if digest != preview.fingerprint:
        preview.status = preview.Status.STALE
        preview.save(update_fields=["status"])
        raise CalendarSyncError(
            "The Google Calendar changed after preview. No changes were made; create and review a fresh preview."
        )
    for action in actions:
        event = AcademicEvent.objects.get(id=action["event_id"])
        if event.revision != action["revision"]:
            preview.status = preview.Status.STALE
            preview.save(update_fields=["status"])
            raise CalendarSyncError(
                "An event changed after preview. No changes were made; create and review a fresh preview."
            )
    completed = []
    try:
        for action in actions:
            event = AcademicEvent.objects.get(id=action["event_id"])
            if action["operation"] == "add":
                remote = service.events().insert(
                    calendarId=connection.calendar_id, body=action["resource"]
                ).execute()
                event.google_event_id = remote["id"]
                event.google_etag = remote.get("etag", "")
                event.save(update_fields=["google_event_id", "google_etag", "updated_at"])
            elif action["operation"] == "update":
                if not action["etag"]:
                    raise CalendarSyncError("Google did not return an event version. Refresh the preview before updating.")
                request = service.events().patch(
                    calendarId=connection.calendar_id,
                    eventId=action["google_event_id"],
                    body=action["resource"],
                )
                request.headers["If-Match"] = action["etag"]
                remote = request.execute()
                event.google_etag = remote.get("etag", "")
                event.save(update_fields=["google_etag", "updated_at"])
            elif action["operation"] == "remove":
                if not action["etag"]:
                    raise CalendarSyncError("Google did not return an event version. Refresh the preview before removal.")
                request = service.events().delete(
                    calendarId=connection.calendar_id,
                    eventId=action["google_event_id"],
                )
                request.headers["If-Match"] = action["etag"]
                request.execute()
                AcademicEvent.objects.filter(google_event_id=action["google_event_id"]).update(
                    google_event_id="", google_etag=""
                )
            completed.append(action["operation"])
    except Exception as exc:
        preview.status = preview.Status.FAILED
        preview.save(update_fields=["status"])
        if isinstance(exc, CalendarSyncError):
            raise
        raise CalendarSyncError(
            f"Calendar sync stopped after {len(completed)} operation(s). Review Google Calendar before retrying."
        ) from exc
    preview.status = preview.Status.APPLIED
    preview.save(update_fields=["status"])
    return actions, warnings
