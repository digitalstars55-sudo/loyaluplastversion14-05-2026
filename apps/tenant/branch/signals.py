from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Branch, BranchConfig


@receiver(post_save, sender=Branch)
def create_branch_config(sender, instance, created, **kwargs):
    """Auto-creates an empty BranchConfig whenever a Branch is first saved."""
    if created:
        BranchConfig.objects.get_or_create(branch=instance)


@receiver(post_save, sender='branch.CoinTransaction')
def wallet_on_coins(sender, instance, created, **kwargs):
    """Движение монет: Wallet-карта должна показать новый баланс."""
    if not created:
        return
    from django.conf import settings
    if not getattr(settings, 'WALLET_PASS_CERT', ''):
        return
    from . import wallet
    client_id = getattr(getattr(instance, 'client_branch', None), 'client_id', None)
    if client_id:
        try:
            wallet.отметить_изменение(client_id)
        except Exception:  # noqa: BLE001 — карта не должна мешать начислению
            wallet.log.warning('wallet: не отметили изменение', exc_info=True)
