from django.urls import path

from .views import (
    BirthdayPrizeView,
    BirthdayStatusView,
    InventoryActivateView,
    InventoryCooldownView,
    InventoryView,
    SuperPrizeView,
)
from .story_views import (
    StoryAccessView,
    StoryActivateView,
    StoryGiftsView,
    StoryGiftView,
    StoryPlayView,
    StorySelectView,
)

urlpatterns = [
    path('inventory/',          InventoryView.as_view(),         name='inventory'),
    path('super-prize/',        SuperPrizeView.as_view(),         name='super-prize'),
    path('inventory/cooldown/', InventoryCooldownView.as_view(),  name='inventory-cooldown'),
    path('inventory/activate/', InventoryActivateView.as_view(),  name='inventory-activate'),
    path('birthday/status/',    BirthdayStatusView.as_view(),     name='birthday-status'),
    path('birthday/prize/',     BirthdayPrizeView.as_view(),      name='birthday-prize'),

    # ── Механика «игра через сториз» (внешние пользователи) ──
    path('story/access/',   StoryAccessView.as_view(),   name='story-access'),
    path('story/play/',     StoryPlayView.as_view(),     name='story-play'),
    path('story/gifts/',    StoryGiftsView.as_view(),    name='story-gifts'),
    path('story/select/',   StorySelectView.as_view(),   name='story-select'),
    path('story/gift/',     StoryGiftView.as_view(),     name='story-gift'),
    path('story/activate/', StoryActivateView.as_view(), name='story-activate'),
]

# Плагин iikoFront «LoyalUP» и ключ точки для него — inventory/pos_plugin.py
from apps.tenant.inventory.pos_plugin import (  # noqa: E402
    BranchPluginKeyView, PluginGiftsView, PluginRedeemView, PluginReleaseView,
)

urlpatterns += [
    path('pos/plugin/gifts',                       PluginGiftsView.as_view(),   name='pos-plugin-gifts'),
    path('pos/plugin/gifts/',                      PluginGiftsView.as_view()),
    path('pos/plugin/gifts/<int:pk>/redeem',       PluginRedeemView.as_view(),  name='pos-plugin-redeem'),
    path('pos/plugin/gifts/<int:pk>/redeem/',      PluginRedeemView.as_view()),
    path('pos/plugin/gifts/<int:pk>/release',      PluginReleaseView.as_view(), name='pos-plugin-release'),
    path('pos/plugin/gifts/<int:pk>/release/',     PluginReleaseView.as_view()),
    path('branches/<int:pk>/pos-plugin-key/',      BranchPluginKeyView.as_view(), name='branch-pos-plugin-key'),
]
