from django.urls import path

from .client_phone import ClientPhoneView
from .client_pos_card import ClientPosCardView
from .client_receipt import ClientReceiptView
from .views import (
    BranchInfoView, ClientView, EmployeeView, PromotionView, TransactionsView,
    TestimonialCreateView, VKAuthView, VKCallbackView, VKStoryView, VKSyncView,
    VKSubscriptionStatusView,
)

urlpatterns = [
    path('branches/<int:branch_id>/', BranchInfoView.as_view(), name='branch-info'),
    path('client/',                   ClientView.as_view(),      name='client'),
    # №78: телефон гостя с согласия через ВК — client_phone.py (префикс /api/v1/client/ уже под подписью запуска)
    path('client/phone/',             ClientPhoneView.as_view(), name='client-phone'),
    # Скан QR фискального чека (волна 2, 14.10.2026) — client_receipt.py
    path('client/receipt/',           ClientReceiptView.as_view(), name='client-receipt'),
    # Карта гостя для кассы, режим iikoCard «Карта гостя» — client_pos_card.py
    path('client/pos-card/',          ClientPosCardView.as_view(), name='client-pos-card'),
    path('client/vk-sync/',           VKSyncView.as_view(),      name='client-vk-sync'),
    path('client/vk-subscription-status/', VKSubscriptionStatusView.as_view(), name='client-vk-subscription-status'),
    path('employees/',                EmployeeView.as_view(),    name='employees'),
    path('promotions/',               PromotionView.as_view(),   name='promotions'),
    path('transactions/',             TransactionsView.as_view(), name='transactions'),
    path('vk/auth/',                  VKAuthView.as_view(),      name='vk-auth'),
    path('vk/story/',                 VKStoryView.as_view(),     name='vk-story'),
    path('vk/callback/',              VKCallbackView.as_view(),  name='vk-callback'),
    path('testimonials/',             TestimonialCreateView.as_view(), name='testimonials-create'),
]
