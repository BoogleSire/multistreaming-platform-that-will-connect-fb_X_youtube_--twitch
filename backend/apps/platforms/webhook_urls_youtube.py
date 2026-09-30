from django.urls import path

from .webhooks import youtube_push_callback, youtube_push_verify

urlpatterns = [
    path("", youtube_push_verify),          # GET hub.challenge handshake
    path("notify", youtube_push_callback),  # POST notifications (HMAC-verified)
]
