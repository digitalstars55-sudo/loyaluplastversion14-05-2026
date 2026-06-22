from django.db import models

from apps.shared.base import TimeStampedModel


class ProductCategory(TimeStampedModel):
    """
    Раздел каталога конкретной торговой точки.
    Позволяет структурировать товары (еда, напитки, мерч и т.д.).
    """

    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='categories',
        verbose_name='Торговая точка',
    )
    name = models.CharField(max_length=255, verbose_name='Название')
    ordering = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Меньшее значение отображается выше.',
        db_index=True,
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Категория'
        verbose_name_plural = 'Категории'
        ordering = ['ordering', 'name']
        indexes = [
            models.Index(fields=['branch', 'ordering'], name='catalog_cat_branch_ord_idx'),
        ]


class Product(TimeStampedModel):
    """
    Подарок из каталога.

    Создаётся один раз и подключается к нужным торговым точкам через ProductBranch.
    Три сценария участия:
      is_super_prize    — входит в пул суперпризов (выбор при достижении)
      is_birthday_prize — предлагается гостю в день рождения
      обычный товар     — доступен для покупки за баллы
    Флаги не взаимоисключающие — товар может участвовать в нескольких сценариях.
    """

    branches = models.ManyToManyField(
        'branch.Branch',
        through='ProductBranch',
        related_name='products',
        verbose_name='Торговые точки',
        blank=True,
    )

    # ── Основные поля ──────────────────────────────────────────────────────

    name = models.CharField(max_length=255, verbose_name='Название')
    description = models.TextField(blank=True, verbose_name='Описание')
    emoji = models.CharField(
        max_length=8, blank=True,
        verbose_name='Эмодзи',
        help_text='Один символ-эмодзи для отображения в мобильном приложении.',
    )
    image = models.ImageField(
        upload_to='catalog/products/',
        blank=True,
        null=True,
        verbose_name='Изображение',
    )
    price = models.PositiveIntegerField(
        verbose_name='Цена (баллы)',
        help_text='Количество баллов, которое списывается при покупке. 0 — бесплатно.',
    )
    cost_price_rub = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name='Себестоимость, ₽',
        help_text=(
            'Реальная стоимость подарка в рублях. Попадает в «Экономику клиента» '
            'по факту активации подарка гостем — берётся снимок себестоимости на '
            'момент активации, поэтому изменение этого поля не меняет прошлые отчёты.'
        ),
    )

    # ── Специальные флаги ──────────────────────────────────────────────────

    is_super_prize = models.BooleanField(
        default=False,
        verbose_name='Суперприз',
        help_text='Входит в пул суперпризов — может быть выдан при достижении цели.',
    )
    is_birthday_prize = models.BooleanField(
        default=False,
        verbose_name='Подарок на ДР',
        help_text='Доступен как поздравительный подарок в день рождения гостя.',
    )
    is_story_prize = models.BooleanField(
        default=False,
        verbose_name='Подарок для игры через сториз',
        help_text=(
            'Входит в отдельный набор подарков для внешних пользователей, '
            'пришедших по сториз. НЕ смешивается с супер-призами основной игры. '
            'Пул для точки = подарки с этим флагом, подключённые к точке.'
        ),
    )
    is_archived = models.BooleanField(
        default=False,
        verbose_name='Архивирован',
        help_text=(
            'Скрытый подарок: не выдаётся в магазине, ДР-пуле и супер-пуле. '
            'У гостей, которые уже получили его в инвентарь, остаётся '
            'доступным к активации — данные не теряются.'
        ),
    )

    def __str__(self):
        return f'{self.name} ({self.price} ★)'

    class Meta:
        verbose_name = 'Подарок'
        verbose_name_plural = 'Подарки'
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_super_prize'],   name='catalog_prod_super_idx'),
            models.Index(fields=['is_birthday_prize'], name='catalog_prod_bday_idx'),
            models.Index(fields=['is_archived'],       name='catalog_prod_archived_idx'),
        ]


class ProductBranch(models.Model):
    """
    Подключение подарка к конкретной торговой точке.
    Хранит per-branch настройки: категорию, порядок и видимость в каталоге.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='branch_assignments',
        verbose_name='Подарок',
    )
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='product_assignments',
        verbose_name='Торговая точка',
    )
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_assignments',
        verbose_name='Категория',
    )
    ordering = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Меньшее значение отображается выше в списке.',
        db_index=True,
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Активен',
        help_text='Неактивный товар скрыт в каталоге призов (магазине), но остаётся доступен как суперприз и именинный приз.',
    )

    def __str__(self):
        return f'{self.product.name} → {self.branch}'

    class Meta:
        verbose_name = 'Назначение в точку'
        verbose_name_plural = 'Назначения в точки'
        unique_together = [('product', 'branch')]
        ordering = ['ordering']
        indexes = [
            models.Index(
                fields=['branch', 'is_active', 'ordering'],
                name='catalog_pb_branch_active_idx',
            ),
            models.Index(
                fields=['branch', 'product'],
                name='catalog_pb_branch_product_idx',
            ),
        ]
