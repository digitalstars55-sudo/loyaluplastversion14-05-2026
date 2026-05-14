from django.urls import path
from .views import (
    GeneralStatsAPIView, RFStatsAPIView, BranchListAPIView,
    RecalculateRFView, RFThresholdsAPIView, SlowStatsAPIView,
    AutoReplySettingsAPIView, EngagementAnalyticsAPIView,
    CampaignsHistoryAPIView,
    SendSegmentBroadcastAPIView, GenerateBroadcastTextAPIView,
    GenerateReportCommentAPIView,
)

urlpatterns = [
    path('analytics/stats/',            GeneralStatsAPIView.as_view(), name='analytics-stats'),
    path('analytics/stats/slow/',       SlowStatsAPIView.as_view(),    name='analytics-stats-slow'),
    path('analytics/rf/',               RFStatsAPIView.as_view(),      name='analytics-rf'),
    path('analytics/rf/recalculate/',   RecalculateRFView.as_view(),   name='analytics-rf-recalculate'),
    path('analytics/rf/thresholds/',    RFThresholdsAPIView.as_view(), name='analytics-rf-thresholds'),
    path('analytics/auto-reply/settings/', AutoReplySettingsAPIView.as_view(), name='analytics-auto-reply-settings'),
    path('analytics/engagement/',          EngagementAnalyticsAPIView.as_view(), name='analytics-engagement'),
    path('analytics/campaigns/',           CampaignsHistoryAPIView.as_view(),    name='analytics-campaigns'),
    path('analytics/rf/send-broadcast/',       SendSegmentBroadcastAPIView.as_view(),    name='analytics-rf-send-broadcast'),
    path('analytics/rf/generate-broadcast-text/', GenerateBroadcastTextAPIView.as_view(), name='analytics-rf-generate-text'),
    path('analytics/report/generate-comment/',    GenerateReportCommentAPIView.as_view(), name='analytics-report-generate-comment'),
    path('analytics/branches/',         BranchListAPIView.as_view(),   name='analytics-branches'),
]
