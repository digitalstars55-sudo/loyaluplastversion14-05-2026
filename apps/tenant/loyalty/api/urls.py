from django.urls import path

from .views import AccrueView, BalanceView, RedeemView, RefundView, SpendView

urlpatterns = [
    path('loyalty/balance', BalanceView.as_view(), name='loyalty-balance'),
    path('loyalty/accrue',  AccrueView.as_view(),  name='loyalty-accrue'),
    path('loyalty/redeem',  RedeemView.as_view(),  name='loyalty-redeem'),
    path('loyalty/refund',  RefundView.as_view(),  name='loyalty-refund'),
    path('loyalty/spend',   SpendView.as_view(),   name='loyalty-spend'),
]
