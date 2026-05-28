from django.db import models


class POSType(models.TextChoices):
    NONE = 'none', 'Не подключено'
    IIKO = 'iiko', 'iiko'
    DOOGLYS = 'dooglys', 'Dooglys'


class ClientConfig(models.Model):
    company = models.OneToOneField(
        'clients.Company',
        on_delete=models.CASCADE,
        related_name='config',
        verbose_name='Компания',
    )

    # --- Брендинг (платный) ---
    logotype_image = models.ImageField(
        upload_to='config/logos/',
        blank=True, null=True,
        verbose_name='Логотип',
        help_text=(
            'Опционально (платный брендинг). Требования: PNG с прозрачным фоном, '
            'квадрат (рекомендуется 512×512 px), сторона 256–2048 px, до 1 МБ.'
        ),
    )
    coin_image = models.ImageField(
        upload_to='config/coins/',
        blank=True, null=True,
        verbose_name='Иконка монеты',
        help_text='Опционально — активируется при подключении платного брендинга',
    )
    brand_color = models.CharField(
        max_length=7,
        default='#d3a9e5',
        verbose_name='Главный цвет бренда (HEX)',
        help_text=(
            'Главный цвет VK мини-приложения в формате #RRGGBB. По нему '
            'автоматически генерируются производные оттенки (тёмный, светлый, '
            'и т.д.) — весь миниапп перекрашивается одним полем. По умолчанию '
            'фиолетовый #d3a9e5.'
        ),
    )
    brand_color_secondary = models.CharField(
        max_length=7,
        default='#d6de23',
        verbose_name='Второй (акцентный) цвет бренда (HEX)',
        help_text=(
            'Акцентный цвет в формате #RRGGBB (по умолчанию лаймовый #d6de23). '
            'Перекрашивает акцентные элементы миниаппа (кнопки действий, бейджи). '
            'У бренда всегда два цвета — задайте оба для целостного вида.'
        ),
    )

    # --- ВКонтакте ---
    vk_group_id = models.PositiveIntegerField(
        verbose_name='VK Group ID',
        help_text='Числовой ID группы ВКонтакте. Отображается на фронте для подписки.',
    )
    vk_group_name = models.CharField(
        max_length=255,
        verbose_name='Название группы VK',
        help_text='Отображается в приложении рядом с кнопкой «Подписаться»',
    )

    # --- Кастомные сообщения для гостей ---
    code_prompt_message = models.TextField(
        blank=True,
        default='ЧТОБЫ ЗАБРАТЬ МОНЕТЫ, ПОПРОСИТЕ КОД ДНЯ У СОТРУДНИКА',
        verbose_name='Подсказка про код монет',
        help_text=(
            'Полный текст подсказки, который видит гость в модалке ввода кода. '
            'Можно писать любую фразу — она показывается целиком. '
            'Точка может переопределить в своих настройках.'
        ),
    )
    quest_show_message = models.TextField(
        blank=True,
        default='У ВАС ЕСТЬ 30 МИНУТ, ЧТОБЫ ВЫПОЛНИТЬ ЗАДАНИЕ И ПОКАЗАТЬ РЕЗУЛЬТАТ СОТРУДНИКУ.',
        verbose_name='Подсказка про показ задания',
        help_text=(
            'Полный текст подсказки в активации задания (квеста). Можно править '
            'свободно — например, заменить «сотруднику» на «администратору» '
            'или сократить «30 минут».'
        ),
    )

    # --- Подарок на день рождения (сетевой уровень, LU-13) ---
    birthday_window_days = models.PositiveSmallIntegerField(
        default=5,
        verbose_name='Окно подарка ДР — на всю сеть (дней ±)',
        help_text=(
            'Значение по умолчанию для всей сети: за сколько дней до и после ДР '
            'гостю доступен подарок. 0 = только в день рождения. Точка может '
            'переопределить в своих настройках; кнопка «Применить ко всем точкам» '
            'проставит это значение всем точкам сразу.'
        ),
    )

    # --- Кассовая система ---
    pos_type = models.CharField(
        max_length=10,
        choices=POSType.choices,
        default=POSType.NONE,
        verbose_name='Кассовая система',
        help_text='Выберите систему, которую использует клиент. Поля ниже активируются автоматически.',
    )

    # IIKO
    iiko_api_url = models.URLField(
        blank=True,
        verbose_name='IIKO API URL',
        help_text='Пример: https://iiko.biz/api/1/',
    )
    iiko_login = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='IIKO Логин',
        help_text='Логин пользователя API из кабинета iiko.',
    )
    iiko_password = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='IIKO Пароль',
        help_text='Пароль пользователя API из кабинета iiko.',
    )

    # Dooglys
    dooglys_api_url = models.URLField(
        blank=True,
        verbose_name='Dooglys API URL',
        help_text='Пример: https://api.dooglys.com/v1/',
    )
    dooglys_api_token = models.CharField(
        max_length=512,
        blank=True,
        verbose_name='Dooglys API Token',
        help_text='API-ключ из личного кабинета Dooglys.',
    )

    def __str__(self):
        return f'Настройки — {self.company.name}'

    class Meta:
        verbose_name = 'Настройки клиента'
        verbose_name_plural = 'Настройки клиентов'
