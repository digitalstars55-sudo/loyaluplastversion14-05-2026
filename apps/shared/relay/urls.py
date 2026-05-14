from django.urls import path

from .views import InboundReplyView

urlpatterns = [
    path('inbound-reply/', InboundReplyView.as_view(), name='loyalup-relay-inbound-reply'),
]