"""
Сетевой вход из каталога VK (новичок без QR).

Живёт в ПУБЛИЧНОЙ схеме — гость заходит в мини-приложение из каталога VK,
не выбрав ни компанию, ни точку. Здесь хранится воронка переходов (DiscoveryEvent)
и единственный приветственный приз гостя (DiscoveryClaim). Сам подарок создаётся
как StoryGiftEntry с источником 'vk_catalog' в схеме выбранного тенанта — забор в
любой точке города по коду дня (та же механика, что вход с сайта).

Воронка (для сводки суперадмина, кросс-тенантно без обхода схем):
    open       — открыл экран сетевого входа
    play       — крутанул колесо
    claim_open — нажал «Забрать», открыл список городов
  + city_chosen = DiscoveryClaim.created_at  (выбрал город — приз создан)
  + redeemed    = DiscoveryClaim.redeemed_at (активировал на кассе по коду дня)
"""

from django.db import models
from django.utils import timezone


class DiscoveryStage(models.TextChoices):
    OPEN       = 'open',       'Открыл сетевой вход'
    PLAY       = 'play',       'Крутанул колесо'
    CLAIM_OPEN = 'claim_open', 'Открыл список городов'


class DiscoveryEvent(models.Model):
    """Событие воронки сетевого входа (open / play / claim_open)."""

    vk_id = models.PositiveIntegerField(db_index=True, verbose_name='VK ID гостя')
    stage = models.CharField(
        max_length=16, choices=DiscoveryStage.choices, db_index=True,
        verbose_name='Стадия воронки',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Когда')

    @classmethod
    def record(cls, vk_id: int, stage: str) -> 'DiscoveryEvent':
        """Best-effort запись события (не валит основной поток)."""
        try:
            return cls.objects.create(vk_id=int(vk_id), stage=stage)
        except Exception:
            return None

    class Meta:
        verbose_name = 'Событие сетевого входа (VK-каталог)'
        verbose_name_plural = 'События сетевого входа (VK-каталог)'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['stage', 'created_at'], name='disc_stage_created_idx'),
        ]


class DiscoveryClaim(models.Model):
    """
    Единственный приветственный приз гостя из каталога VK.

    Уникальность по vk_id гарантирует «1 приз на человека — в один город».
    Хранит выбранный город (тенант) и точку-«дом» подарка (на ней создан
    StoryGiftEntry). redeemed_at заполняется при активации на кассе.
    """

    vk_id = models.PositiveIntegerField(unique=True, verbose_name='VK ID гостя')
    company = models.ForeignKey(
        'clients.Company',
        on_delete=models.CASCADE,
        related_name='discovery_claims',
        verbose_name='Выбранная сеть (тенант)',
    )
    city = models.CharField(max_length=100, blank=True, default='', verbose_name='Город')
    home_branch_id = models.PositiveIntegerField(
        verbose_name='Точка-«дом» подарка',
        help_text='branch_id точки, на которой создан StoryGiftEntry новичка.',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Выбрал город')
    redeemed_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        verbose_name='Активировал на кассе',
    )
    redeemed_branch_id = models.PositiveIntegerField(
        null=True, blank=True,
        verbose_name='Точка активации',
        help_text='branch_id точки, где забрали по коду дня.',
    )

    def mark_redeemed(self, branch_id: int | None = None) -> None:
        self.redeemed_at = timezone.now()
        if branch_id:
            self.redeemed_branch_id = branch_id
        self.save(update_fields=['redeemed_at', 'redeemed_branch_id'])

    class Meta:
        verbose_name = 'Приз новичка (VK-каталог)'
        verbose_name_plural = 'Призы новичков (VK-каталог)'
        ordering = ['-created_at']
