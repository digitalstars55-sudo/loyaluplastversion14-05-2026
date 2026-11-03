from django.urls import path

from apps.shared.guest.request_log import ClientLogView

from .client_phone import ClientPhoneView
from .client_pos_card import ClientPosCardView
from .client_receipt import ClientReceiptView
from .review_proofs import (
    ClientReviewProofView, ReviewProofApproveView, ReviewProofListView, ReviewProofRejectView, ReviewProofResendView,
)
from .views import (
    BranchInfoView, ClientView, EmployeeView, PromotionView, TransactionsView,
    ClientForgetView, TelegramPhoneLinkView, TelegramSessionView, TestimonialCreateView, VKAuthView, VKCallbackView, VKStoryView, VKSyncView,
    VKSubscriptionStatusView,
)

from .loyalty_extra_views import (
    CertificateDesignDetailView, CertificateDesignGenerateView, CertificateDesignListView, CertificateListView,
    CertificatePublicView, CertificateRedeemView, CertificateSettingsView, CertificateVoidView, ClientLoyaltyExtraView,
)
from .wallet_views import ClientWalletView, ИзменившиесяView, ЖурналView, КартаДляТелефонаView, РегистрацияView, СкачатьКартуView

urlpatterns = [
    path('branches/<int:branch_id>/', BranchInfoView.as_view(), name='branch-info'),
    path('client/',                   ClientView.as_view(),      name='client'),
    # №78: телефон гостя с согласия через ВК — client_phone.py (префикс /api/v1/client/ уже под подписью запуска)
    path('client/phone/',             ClientPhoneView.as_view(), name='client-phone'),
    # Скан QR фискального чека (волна 2, 14.10.2026) — client_receipt.py
    path('client/receipt/',           ClientReceiptView.as_view(), name='client-receipt'),
    # Скриншот отзыва на картах (волна 3, 14.10.2026) — review_proofs.py
    path('client/review-proof/',      ClientReviewProofView.as_view(), name='client-review-proof'),
    path('review-proofs/',            ReviewProofListView.as_view(), name='review-proofs'),
    path('review-proofs/<int:pk>/approve/', ReviewProofApproveView.as_view(), name='review-proof-approve'),
    path('review-proofs/<int:pk>/reject/',  ReviewProofRejectView.as_view(), name='review-proof-reject'),
    path('review-proofs/<int:pk>/resend/',  ReviewProofResendView.as_view(), name='review-proof-resend'),
    # Карта гостя для кассы, режим iikoCard «Карта гостя» — client_pos_card.py
    # Этап 1 (02.11.2026): уровень и приглашение гостя, подарочные сертификаты — loyalty_extra_views.py
    path('client/loyalty-extra/', ClientLoyaltyExtraView.as_view(), name='client-loyalty-extra'),
    path('certificates/', CertificateListView.as_view(), name='certificates'),
    path('certificates/settings/', CertificateSettingsView.as_view(), name='certificate-settings'),
    path('certificates/designs/', CertificateDesignListView.as_view(), name='certificate-designs'),
    path('certificates/designs/generate/', CertificateDesignGenerateView.as_view(), name='certificate-designs-generate'),
    path('certificates/designs/<int:pk>/', CertificateDesignDetailView.as_view(), name='certificate-design'),
    path('certificates/<int:pk>/redeem/', CertificateRedeemView.as_view(), name='certificate-redeem'),
    path('certificates/<int:pk>/void/', CertificateVoidView.as_view(), name='certificate-void'),
    path('public/certificate/<str:code>/', CertificatePublicView.as_view(), name='certificate-public'),
    path('client/wallet/',            ClientWalletView.as_view(), name='client-wallet'),
    path('wallet/pass/<str:serial>/', СкачатьКартуView.as_view(), name='wallet-pass'),
    path('wallet/apple/v1/devices/<str:device>/registrations/<str:pass_type>/<str:serial>', РегистрацияView.as_view(), name='wallet-register'),
    path('wallet/apple/v1/devices/<str:device>/registrations/<str:pass_type>', ИзменившиесяView.as_view(), name='wallet-updated'),
    path('wallet/apple/v1/passes/<str:pass_type>/<str:serial>', КартаДляТелефонаView.as_view(), name='wallet-latest'),
    path('wallet/apple/v1/log', ЖурналView.as_view(), name='wallet-log'),
    path('client/pos-card/',          ClientPosCardView.as_view(), name='client-pos-card'),
    path('client/vk-sync/',           VKSyncView.as_view(),      name='client-vk-sync'),
    path('client/vk-subscription-status/', VKSubscriptionStatusView.as_view(), name='client-vk-subscription-status'),
    path('employees/',                EmployeeView.as_view(),    name='employees'),
    path('promotions/',               PromotionView.as_view(),   name='promotions'),
    path('transactions/',             TransactionsView.as_view(), name='transactions'),
    path('client-log/',              ClientLogView.as_view(),   name='client-log'),
    path('vk/auth/',                  VKAuthView.as_view(),      name='vk-auth'),
    path('vk/tg-session/',            TelegramSessionView.as_view(), name='vk-tg-session'),
    path('vk/tg-phone-link/',         TelegramPhoneLinkView.as_view(), name='vk-tg-phone-link'),
    path('client/forget/',            ClientForgetView.as_view(), name='client-forget'),
    path('vk/story/',                 VKStoryView.as_view(),     name='vk-story'),
    path('vk/callback/',              VKCallbackView.as_view(),  name='vk-callback'),
    path('testimonials/',             TestimonialCreateView.as_view(), name='testimonials-create'),
]
