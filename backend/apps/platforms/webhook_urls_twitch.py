from django.urls import path

from .webhooks import twitch_eventsub

urlpatterns = [path("", twitch_eventsub)]
