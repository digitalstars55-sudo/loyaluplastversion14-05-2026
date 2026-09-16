from django.urls import path
from .summary import DashboardTodayAPIView, ReviewsSummaryAPIView, StatsDetailAPIView
from .views import (
    GeneralStatsAPIView, RFStatsAPIView, BranchListAPIView,
    RecalculateRFView, RFThresholdsAPIView, SlowStatsAPIView,
    AutoReplySettingsAPIView, EngagementAnalyticsAPIView,
    CampaignsHistoryAPIView, CampaignDetailAPIView,
    RFSegmentListAPIView,
    SendSegmentBroadcastAPIView, GenerateBroadcastTextAPIView,
    GenerateReportCommentAPIView, RFMigrationsListAPIView,
    LoyaltyReportAPIView, ContactPointsAPIView,
    RFMRewardCatalogAPIView, RFMCampaignAPIView, RFMCampaignDetailAPIView,
    RFMCampaignCancelAPIView, RFMCampaignKPIAPIView,
)

urlpatterns = [
    path('analytics/stats/',            GeneralStatsAPIView.as_view(), name='analytics-stats'),
    path('analytics/report/',           LoyaltyReportAPIView.as_view(), name='analytics-report-json'),
    path('analytics/stats/slow/',       SlowStatsAPIView.as_view(),    name='analytics-stats-slow'),
    path('analytics/rf/',               RFStatsAPIView.as_view(),      name='analytics-rf'),
    path('analytics/rf/migrations/',    RFMigrationsListAPIView.as_view(), name='analytics-rf-migrations'),
    path('analytics/rf/recalculate/',   RecalculateRFView.as_view(),   name='analytics-rf-recalculate'),
    path('analytics/rf/thresholds/',    RFThresholdsAPIView.as_view(), name='analytics-rf-thresholds'),
    path('analytics/auto-reply/settings/', AutoReplySettingsAPIView.as_view(), name='analytics-auto-reply-settings'),
    path('analytics/engagement/',          EngagementAnalyticsAPIView.as_view(), name='analytics-engagement'),
    path('analytics/campaigns/',           CampaignsHistoryAPIView.as_view(),    name='analytics-campaigns'),
    path('analytics/campaigns/<int:pk>/', CampaignDetailAPIView.as_view(),       name='analytics-campaign-detail'),
    path('analytics/segments/',           RFSegmentListAPIView.as_view(),       name='analytics-segments'),
    path('analytics/rf/send-broadcast/',       SendSegmentBroadcastAPIView.as_view(),    name='analytics-rf-send-broadcast'),
    path('analytics/rf/reward-catalog/',       RFMRewardCatalogAPIView.as_view(),        name='analytics-rf-reward-catalog'),
    path('analytics/rf/campaigns/',            RFMCampaignAPIView.as_view(),             name='analytics-rf-campaigns'),
    path('analytics/rf/campaigns/kpi/',        RFMCampaignKPIAPIView.as_view(),          name='analytics-rf-campaigns-kpi'),
    path('analytics/rf/campaigns/<int:pk>/',   RFMCampaignDetailAPIView.as_view(),       name='analytics-rf-campaign-detail'),
    path('analytics/rf/campaigns/<int:pk>/cancel/', RFMCampaignCancelAPIView.as_view(),  name='analytics-rf-campaign-cancel'),
    path('analytics/rf/generate-broadcast-text/', GenerateBroadcastTextAPIView.as_view(), name='analytics-rf-generate-text'),
    path('analytics/report/generate-comment/',    GenerateReportCommentAPIView.as_view(), name='analytics-report-generate-comment'),
    path('analytics/branches/',         BranchListAPIView.as_view(),   name='analytics-branches'),
    # Экраны CheckUp (контракт платформы, ручки №2/№5/№4) — только чтение, цифры как в веб-кабинете.
    path('analytics/reviews/summary/',  ReviewsSummaryAPIView.as_view(), name='analytics-reviews-summary'),
    path('analytics/stats/detail/',     StatsDetailAPIView.as_view(),    name='analytics-stats-detail-api'),
    path('dashboard/today/',            DashboardTodayAPIView.as_view(), name='dashboard-today'),
    path('analytics/contact-points/',   ContactPointsAPIView.as_view(), name='analytics-contact-points-api'),
]
