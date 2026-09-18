"""
Маршруты комментариев и печатной версии отчёта (контракт 3б.4, №28).

Отдельный urls-модуль: подключение стоит одной строки в main/urls.py.
Живые `analytics/report/` и `analytics/report/generate-comment/` не задеваются —
у них свои маршруты в analytics/api/urls.py.
"""
from django.urls import path

from .report_comments import (
    GenerateReportCommentSaveAPIView,
    ReportCommentsAPIView,
    ReportPrintView,
    ReportSectionsAPIView,
)

urlpatterns = [
    path('analytics/report/sections/', ReportSectionsAPIView.as_view(),
         name='analytics-report-sections'),
    path('analytics/report/comments/', ReportCommentsAPIView.as_view(),
         name='analytics-report-comments'),
    path('analytics/report/comments/generate/', GenerateReportCommentSaveAPIView.as_view(),
         name='analytics-report-comments-generate'),
    path('analytics/report/print/', ReportPrintView.as_view(),
         name='analytics-report-print'),
]
