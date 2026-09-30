"""Clips Studio URL routing (§31). Mounted under /api/v1/streams/{stream_id}/clips.

NOTE: these paths are intentionally NOT nested inside apps.streams.urls (that module is not
part of this change set and does not exist yet); they live at the top level of the versioned
API so the feature works independently of the streams router landing together.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("<uuid:stream_id>/clips/", views.clips_collection, name="clip-list-create"),
    path("<uuid:stream_id>/clips/<uuid:clip_id>/", views.clip_detail, name="clip-detail"),
    path("<uuid:stream_id>/clips/<uuid:clip_id>/regenerate", views.clip_detail,
         name="clip-regenerate"),
]
