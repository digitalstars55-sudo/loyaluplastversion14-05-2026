from django.urls import path

from .views import (
    BranchInfoView, ClientView, EmployeeView, PromotionView, TransactionsView,
    TestimonialCreateView, VKAuthView, VKCallbackView, VKStoryView, VKSyncView,
    VKSubscriptionStatusView,
)

urlpatterns = [
    path('branches/<int:branch_id>/', BranchInfoView.as_view(), name='branch-info'),
    path('client/',                   ClientView.as_view(),      name='client'),
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
