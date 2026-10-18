from django.urls import path

from planner import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("courses/new/", views.course_create, name="course_create"),
    path("courses/<uuid:course_id>/", views.course_detail, name="course_detail"),
    path("courses/<uuid:course_id>/syllabus/", views.save_syllabus, name="save_syllabus"),
    path("courses/<uuid:course_id>/extract/", views.extract_syllabus, name="extract_syllabus"),
    path("courses/<uuid:course_id>/events/add/", views.event_create, name="event_create"),
    path(
        "courses/<uuid:course_id>/events/<uuid:event_id>/edit/",
        views.event_update,
        name="event_update",
    ),
    path(
        "courses/<uuid:course_id>/events/<uuid:event_id>/approve/",
        views.event_approve,
        name="event_approve",
    ),
    path(
        "courses/<uuid:course_id>/events/<uuid:event_id>/delete/",
        views.event_delete,
        name="event_delete",
    ),
    path("google/", views.google_settings, name="google_settings"),
    path("google/connect/", views.google_connect, name="google_connect"),
    path("google/callback/", views.google_callback, name="google_callback"),
    path("google/disconnect/", views.google_disconnect, name="google_disconnect"),
    path(
        "courses/<uuid:course_id>/sync/preview/",
        views.sync_preview_create,
        name="sync_preview_create",
    ),
    path("sync/<uuid:preview_id>/", views.sync_preview_detail, name="sync_preview_detail"),
    path("sync/<uuid:preview_id>/apply/", views.sync_preview_apply, name="sync_preview_apply"),
    path(
        "courses/<uuid:course_id>/delete/",
        views.privacy_delete_course,
        name="privacy_delete_course",
    ),
]
