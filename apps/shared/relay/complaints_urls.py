from django.urls import path

from .verdict import ComplaintVerdictView

urlpatterns = [
    path('verdict/', ComplaintVerdictView.as_view(), name='loyalup-complaint-verdict'),
]
