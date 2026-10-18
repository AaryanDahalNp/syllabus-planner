import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.db import models


class Course(models.Model):
    class DateConvention(models.TextChoices):
        MDY = "MDY", "Month / day / year"
        DMY = "DMY", "Day / month / year"
        UNSPECIFIED = "UNSPECIFIED", "Not specified"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    term_start = models.DateField(null=True, blank=True)
    term_end = models.DateField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default="America/New_York")
    date_convention = models.CharField(
        max_length=12,
        choices=DateConvention.choices,
        default=DateConvention.UNSPECIFIED,
    )
    syllabus_text = models.TextField(blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.term_start and self.term_end and self.term_end < self.term_start:
            raise ValidationError({"term_end": "Term end must be on or after the start date."})

    def __str__(self):
        return self.name


class ExtractionAttempt(models.Model):
    class Status(models.TextChoices):
        STARTED = "started", "Started"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="extraction_attempts")
    provider = models.CharField(max_length=24, default="gemini")
    model = models.CharField(max_length=120)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.STARTED)
    error_category = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class AcademicEvent(models.Model):
    class Kind(models.TextChoices):
        EXAM = "exam", "Exam"
        ASSIGNMENT = "assignment", "Assignment"
        QUIZ = "quiz", "Quiz"
        PROJECT = "project", "Project"
        CLASS = "class", "Class"
        HOLIDAY = "holiday", "Holiday / break"
        OTHER = "other", "Other"

    class Action(models.TextChoices):
        EVENT = "event", "Scheduled event"
        CANCEL = "cancel", "Cancellation / no class / no assignment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="events")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    action = models.CharField(max_length=12, choices=Action.choices, default=Action.EVENT)
    start_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    date_text = models.CharField(max_length=200, blank=True)
    source_excerpt = models.CharField(max_length=600, blank=True)
    source_page = models.PositiveSmallIntegerField(null=True, blank=True)
    source_fingerprint = models.CharField(max_length=64, blank=True)
    issues = models.JSONField(default=list, blank=True)
    provenance = models.CharField(max_length=12, default="manual")
    approved = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    google_event_id = models.CharField(max_length=255, blank=True)
    google_etag = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_date", "start_time", "title"]

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})
        if self.start_time and not self.start_date:
            raise ValidationError({"start_time": "Add a date before setting a time."})
        if self.end_time and not self.start_time:
            raise ValidationError({"end_time": "An end time requires a start time."})
        if (
            self.start_date
            and self.start_time
            and self.end_time
            and (self.end_date or self.start_date) == self.start_date
            and self.end_time <= self.start_time
        ):
            raise ValidationError({"end_time": "End time must be after start time."})
        if self.action == self.Action.CANCEL and not self.start_date:
            raise ValidationError({"start_date": "A cancellation needs a confirmed date."})

    @property
    def blocking_issues(self):
        issues = list(self.issues or [])
        if not self.start_date:
            issues.append("Date is unknown; add a date before approval.")
        if self.start_time and not self.end_time and self.action == self.Action.EVENT:
            issues.append("Add an end time before syncing a timed event to Google Calendar.")
        current_source = hashlib.sha256(self.course.syllabus_text.encode("utf-8")).hexdigest()
        if self.provenance == "gemini" and self.source_fingerprint != current_source:
            issues.append("Syllabus text changed; verify this event against the current source before approval.")
        return issues

    @property
    def source_changed(self):
        return any(issue.startswith("Syllabus text changed;") for issue in self.blocking_issues)

    def __str__(self):
        return self.title


class GoogleConnection(models.Model):
    token_json = models.TextField()
    calendar_id = models.CharField(max_length=255, default="primary")
    connected_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.calendar_id


class SyncPreview(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting confirmation"
        APPLIED = "applied", "Applied"
        STALE = "stale", "Calendar changed; review again"
        FAILED = "failed", "Partially applied or failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="sync_previews")
    calendar_id = models.CharField(max_length=255, default="primary")
    fingerprint = models.CharField(max_length=64)
    actions = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
