from django.urls import path
from .views import (
    GeneralStatsView, ReviewsAnalyticsView, ReviewsDetailView,
    ReviewsReplyView, ReviewsAIReplyView,
    RFAnalysisView, RFMigrationView, StatsDetailView,
    SegmentExportSenlerView, SegmentCreateBroadcastView,
    CreateBroadcastForAllGuestsView,
    LoyaltyReportView,
)

urlpatterns = [
    path('',                  GeneralStatsView.as_view(),     name='analytics-general'),
    path('report/',           LoyaltyReportView.as_view(),    name='analytics-report'),
    path('rf/',               RFAnalysisView.as_view(),       name='analytics-rf'),
    path('rf/migration/',     RFMigrationView.as_view(),      name='analytics-rf-migration'),
    path('rf/broadcast-all/', CreateBroadcastForAllGuestsView.as_view(), name='analytics-broadcast-all'),
    path('rf/segment/<int:segment_id>/export-senler/',   SegmentExportSenlerView.as_view(),    name='analytics-segment-export-senler'),
    path('rf/segment/<int:segment_id>/create-broadcast/', SegmentCreateBroadcastView.as_view(), name='analytics-segment-create-broadcast'),
    path('reviews/',          ReviewsAnalyticsView.as_view(), name='analytics-reviews'),
    path('stats/detail/',     StatsDetailView.as_view(),      name='analytics-stats-detail'),
    path('reviews/detail/',   ReviewsDetailView.as_view(),    name='analytics-reviews-detail'),
    path('reviews/reply/',    ReviewsReplyView.as_view(),     name='analytics-reviews-reply'),
    path('reviews/ai-reply/', ReviewsAIReplyView.as_view(),   name='analytics-reviews-ai-reply'),
]