from django.urls import path

from .views import (
    LeadChatAPIView,
    LeadDetailAPIView,
    LeadListCreateAPIView,
    LeadSubmitAPIView,
)

urlpatterns = [
    path('leads/',                              LeadListCreateAPIView.as_view(), name='leads-create'),
    path('leads/<str:session_token>/',          LeadDetailAPIView.as_view(),     name='leads-detail'),
    path('leads/<str:session_token>/submit/',   LeadSubmitAPIView.as_view(),     name='leads-submit'),
    path('leads/<str:session_token>/chat/',     LeadChatAPIView.as_view(),       name='leads-chat'),
]
