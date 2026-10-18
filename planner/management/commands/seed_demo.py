from datetime import date

from django.core.management.base import BaseCommand

from planner.models import AcademicEvent, Course


class Command(BaseCommand):
    help = "Create a clearly labeled synthetic syllabus planning demo."

    def handle(self, *args, **options):
        course, created = Course.objects.get_or_create(
            name="CS 101 - Synthetic Demo",
            defaults={
                "term_start": date(2026, 10, 19),
                "term_end": date(2026, 12, 18),
                "timezone": "America/New_York",
                "syllabus_text": (
                    "Synthetic demonstration syllabus.\n"
                    "Assignment 1: October 23, 2026 at 5:00 PM.\n"
                    "Midterm exam: October 26, 2026.\n"
                    "No class: November 26, 2026."
                ),
            },
        )
        if created:
            for title, kind, start, action in [
                ("Assignment 1", AcademicEvent.Kind.ASSIGNMENT, date(2026, 10, 23), "event"),
                ("Midterm exam", AcademicEvent.Kind.EXAM, date(2026, 10, 26), "event"),
                ("No class", AcademicEvent.Kind.CLASS, date(2026, 11, 26), "cancel"),
            ]:
                AcademicEvent.objects.create(
                    course=course,
                    title=title,
                    kind=kind,
                    action=action,
                    start_date=start,
                    date_text=start.strftime("%B %d, %Y"),
                    source_excerpt=f"Synthetic demo only: {title}, {start.isoformat()}.",
                    provenance="demo",
                    approved=True,
                )
        self.stdout.write(self.style.SUCCESS(f"Demo course ready: {course.name} (synthetic data only)"))
