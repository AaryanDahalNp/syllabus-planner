from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.exceptions import ValidationError

from .models import AcademicEvent, Course


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["name", "term_start", "term_end", "timezone", "date_convention"]
        help_texts = {
            "timezone": "IANA timezone used when translating timed events, e.g. America/New_York.",
            "date_convention": "Used to flag ambiguous numeric dates such as 03/04.",
            "term_end": "Events outside the term dates are flagged for review.",
        }
        widgets = {
            "term_start": forms.DateInput(attrs={"type": "date"}),
            "term_end": forms.DateInput(attrs={"type": "date"}),
        }

    def clean_timezone(self):
        value = self.cleaned_data["timezone"]
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("Enter a valid IANA timezone, such as America/New_York.")
        return value


class SyllabusForm(forms.Form):
    text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "placeholder": "Paste syllabus text here..."}),
    )
    pdf = forms.FileField(required=False)

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("text", "").strip()
        pdf = cleaned.get("pdf")
        if not text and not pdf:
            raise ValidationError("Paste syllabus text or choose a searchable PDF.")
        if pdf:
            if pdf.size > 5 * 1024 * 1024:
                self.add_error("pdf", "PDFs must be 5 MiB or smaller.")
            if not pdf.name.lower().endswith(".pdf"):
                self.add_error("pdf", "Only PDF files are supported.")
        if len(text) > 60000:
            self.add_error("text", "Syllabus text must be 60,000 characters or fewer.")
        return cleaned


class AcademicEventForm(forms.ModelForm):
    class Meta:
        model = AcademicEvent
        fields = [
            "title",
            "kind",
            "action",
            "start_date",
            "start_time",
            "end_date",
            "end_time",
            "date_text",
            "source_excerpt",
            "source_page",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "date_text": forms.TextInput(attrs={"placeholder": "e.g. Monday, October 26 at 2 PM"}),
        }
        help_texts = {
            "end_date": "Inclusive final date for a multi-day, all-day event. Leave blank for a single-day event.",
            "end_time": "Timed events need both a start and end time; no duration is guessed.",
        }

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        start_time = cleaned.get("start_time")
        end_time = cleaned.get("end_time")
        if end_date and start_date and end_date < start_date:
            self.add_error("end_date", "End date cannot be before start date.")
        if start_time and not end_time and cleaned.get("action") == AcademicEvent.Action.EVENT:
            self.add_error("end_time", "Add an end time before syncing a timed event.")
        if end_time and not start_time:
            self.add_error("start_time", "Add a start time before setting an end time.")
        if start_time and not start_date:
            self.add_error("start_date", "Add a date before setting a start time.")
        if (
            start_date
            and start_time
            and end_time
            and (end_date or start_date) == start_date
            and end_time <= start_time
        ):
            self.add_error("end_time", "End time must be after start time.")
        if cleaned.get("action") == AcademicEvent.Action.CANCEL and not start_date:
            self.add_error("start_date", "A cancellation needs a confirmed date.")
        return cleaned


class GoogleCalendarForm(forms.Form):
    calendar_id = forms.CharField(
        max_length=255,
        initial="primary",
        help_text="Use primary, or a calendar ID from Google Calendar settings.",
    )
