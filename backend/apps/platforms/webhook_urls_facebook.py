from django.urls import path

from .webhooks import facebook_webhook

urlpatterns = [path("", facebook_webhook)]
