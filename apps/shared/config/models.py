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

    # --- Механика «игра через сториз» (внешние пользователи, сетевой уровень) ---
    story_game_enabled = models.BooleanField(
        default=False,
        verbose_name='Игра через сториз включена',
        help_text='Разрешить внешним пользователям, пришедшим по сториз, играть и получать подарок. По умолчанию выключено.',
    )
    story_min_order_amount = models.PositiveIntegerField(
        default=600,
        verbose_name='Мин. сумма заказа для подарка из сториз, ₽',
        help_text='Минимальная сумма заказа для активации подарка из сториз. Подставляется в текст условий. 0 — без ограничения.',
    )
    story_activation_minutes = models.PositiveIntegerField(
        default=40,
        verbose_name='Время действия подарка из сториз после активации, мин',
        help_text='Сколько минут действует подарок после активации в кафе. По умолчанию 40.',
    )
    story_require_cafe_visit = models.BooleanField(
        default=True,
        verbose_name='Требовать визит в кафе (код дня) перед активацией',
        help_text=(
            'По умолчанию включено. Подарок из сториз активируется ТОЛЬКО после ввода '
            'кода дня в кафе — домашняя активация показывает инструкцию и не запускает '
            'таймер. Выключать не рекомендуется.'
        ),
    )
    story_cafe_address = models.CharField(
        max_length=255, blank=True, default='',
        verbose_name='Адрес кафе для подарка из сториз',
        help_text='Куда направляем пользователя за подарком. Пусто — берётся адрес точки.',
    )
    story_activation_text = models.TextField(
        blank=True, default='',
        verbose_name='Текст инструкции перед активацией (сториз)',
        help_text=(
            'Что видит пользователь при попытке активировать подарок до визита в кафе. '
            'Поддерживаются переменные: [адрес кафе], [сумма], [время], [название кафе], '
            '[название подарка]. Пусто — стандартный текст по умолчанию.'
        ),
    )
    story_saved_text = models.TextField(
        blank=True, default='',
        verbose_name='Текст уведомления после выбора подарка (сториз)',
        help_text='Сообщение «подарок сохранён в Мои подарки». Пусто — стандартный текст по умолчанию.',
    )
    story_campaign_start = models.DateField(
        null=True, blank=True,
        verbose_name='Дата начала кампании сториз',
        help_text='Опционально. До этой даты игра через сториз недоступна. Пусто — без ограничения.',
    )
    story_campaign_end = models.DateField(
        null=True, blank=True,
        verbose_name='Дата окончания кампании сториз',
        help_text='Опционально. После этой даты игра через сториз недоступна. Пусто — без ограничения.',
    )

    # --- Сетевой вход из каталога VK (новичок без QR) ---
    vk_catalog_enabled = models.BooleanField(
        default=False,
        verbose_name='Показывать в каталоге VK (сетевой вход)',
        help_text=(
            'Участвовать в сетевой игре для новичков, заходящих в мини-приложение '
            'из каталога VK (без QR). Город этой сети попадёт в список выбора, а '
            'новичок сможет выиграть приветственный подарок и забрать его в любой '
            'точке сети по коду дня. Нужен подарок с флагом «Приветственный '
            'подарок новичка» в каталоге. По умолчанию выключено.'
        ),
    )
    vk_catalog_city = models.CharField(
        max_length=100, blank=True, default='',
        verbose_name='Город (для каталога VK)',
        help_text='Название города этой сети, как оно показывается новичку в списке выбора (напр. «Брянск»).',
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
