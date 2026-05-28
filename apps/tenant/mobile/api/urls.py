"""
Мобильные API маршруты. Подключаются из main/urls.py с префиксом 'api/v1/'.
"""

from django.urls import path

from .views import (
    MobileBranchListAPIView,
    MobileReviewListAPIView,
    MobileReviewMessagesAPIView,
    MobileReviewReplyAPIView,
    MobileReviewResolveAPIView,
    GuestBirthdaysAPIView,
    GuestDetailAPIView,
    AdjustGuestCoinsAPIView,
    DailyCodesListAPIView,
    GenerateDailyCodeAPIView,
    RegenerateReviewDraftAPIView,
    RejectReviewDraftAPIView,
    GlobalSearchAPIView,
    AuditLogAPIView,
    SubscriptionStatusAPIView,
    BillingPayAPIView,
    StaffListAPIView,
    StaffDetailAPIView,
    StaffInviteAPIView,
    ProductCategoryListCreateAPIView, ProductCategoryDetailAPIView,
    ProductListCreateAPIView, ProductDetailAPIView,
    QuestListCreateAPIView, QuestDetailAPIView,
    PromotionListCreateAPIView, PromotionDetailAPIView,
    SupportChatManagerAPIView, SupportChatMessagesAPIView,
)

urlpatterns = [
    # Branches
    path(
        'mobile/branches/',
        MobileBranchListAPIView.as_view(),
        name='mobile-branch-list',
    ),

    # Guests
    path(
        'guests/birthdays/',
        GuestBirthdaysAPIView.as_view(),
        name='guests-birthdays',
    ),
    path(
        'guests/<int:vk_id>/',
        GuestDetailAPIView.as_view(),
        name='guest-detail',
    ),
    path(
        'guests/<int:vk_id>/adjust-coins/',
        AdjustGuestCoinsAPIView.as_view(),
        name='guest-adjust-coins',
    ),

    # Daily codes (read + manual generate)
    path(
        'branch/daily-codes/',
        DailyCodesListAPIView.as_view(),
        name='branch-daily-codes',
    ),
    path(
        'branch/daily-codes/generate/',
        GenerateDailyCodeAPIView.as_view(),
        name='branch-daily-codes-generate',
    ),

    # AI drafts for review replies
    path(
        'analytics/reviews/<int:review_id>/regenerate-draft/',
        RegenerateReviewDraftAPIView.as_view(),
        name='analytics-review-regenerate-draft',
    ),
    path(
        'analytics/reviews/<int:review_id>/reject-draft/',
        RejectReviewDraftAPIView.as_view(),
        name='analytics-review-reject-draft',
    ),

    # Global search
    path(
        'search/',
        GlobalSearchAPIView.as_view(),
        name='global-search',
    ),

    # Audit log
    path(
        'audit-log/',
        AuditLogAPIView.as_view(),
        name='audit-log',
    ),

    # Subscription / billing
    path(
        'billing/status/',
        SubscriptionStatusAPIView.as_view(),
        name='billing-status',
    ),
    path(
        'billing/pay/',
        BillingPayAPIView.as_view(),
        name='billing-pay',
    ),

    # Staff
    path(
        'staff/',
        StaffListAPIView.as_view(),
        name='staff-list',
    ),
    path(
        'staff/<int:staff_id>/',
        StaffDetailAPIView.as_view(),
        name='staff-detail',
    ),
    path(
        'staff/invite/',
        StaffInviteAPIView.as_view(),
        name='staff-invite',
    ),

    # Catalog: categories CRUD
    path('catalog/categories/',           ProductCategoryListCreateAPIView.as_view(), name='catalog-categories'),
    path('catalog/categories/<int:pk>/',  ProductCategoryDetailAPIView.as_view(),     name='catalog-category-detail'),

    # Catalog: products CRUD
    path('catalog/products/',           ProductListCreateAPIView.as_view(), name='catalog-products'),
    path('catalog/products/<int:pk>/',  ProductDetailAPIView.as_view(),     name='catalog-product-detail'),

    # Quests CRUD (overrides /api/v1/ quests if any — name collision OK)
    path('quests/',           QuestListCreateAPIView.as_view(), name='mobile-quests'),
    path('quests/<int:pk>/',  QuestDetailAPIView.as_view(),     name='mobile-quest-detail'),

    # Promotions CRUD (mobile-namespaced)
    path('branch/promotions/',          PromotionListCreateAPIView.as_view(), name='mobile-promotions'),
    path('branch/promotions/<int:pk>/', PromotionDetailAPIView.as_view(),     name='mobile-promotion-detail'),

    # Support chat
    path('support/chat/manager/',  SupportChatManagerAPIView.as_view(),  name='support-chat-manager'),
    path('support/chat/messages/', SupportChatMessagesAPIView.as_view(), name='support-chat-messages'),

    # Reviews
    path(
        'mobile/reviews/',
        MobileReviewListAPIView.as_view(),
        name='mobile-review-list',
    ),
    path(
        'mobile/reviews/<int:review_id>/messages/',
        MobileReviewMessagesAPIView.as_view(),
        name='mobile-review-messages',
    ),
    path(
        'mobile/reviews/<int:review_id>/reply/',
        MobileReviewReplyAPIView.as_view(),
        name='mobile-review-reply',
    ),
    path(
        'mobile/reviews/<int:review_id>/resolve/',
        MobileReviewResolveAPIView.as_view(),
        name='mobile-review-resolve',
    ),
]
