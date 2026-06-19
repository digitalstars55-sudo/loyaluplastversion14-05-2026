"""
Сериализаторы для мобильного API. Все поля соответствуют интерфейсам
в `levelup-back-mobile/rf-mobile/src/types.ts`.

ВАЖНО: модели не правим. Если поле отсутствует — сериализатор отдаёт
безопасное значение по умолчанию (null/false/empty), мобайл готов это
проглотить.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.tenant.branch.models import (
    Branch,
    TestimonialConversation,
    TestimonialMessage,
)


# ════════════════════════════════════════════════════════════════════
# Branch (точка)
# ════════════════════════════════════════════════════════════════════
class BranchSerializer(serializers.ModelSerializer):
    address = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = ['id', 'branch_id', 'name', 'is_active', 'address']
        read_only_fields = fields

    def get_address(self, obj) -> str | None:
        cfg = getattr(obj, 'config', None)
        return getattr(cfg, 'address', None) if cfg else None


# ════════════════════════════════════════════════════════════════════
# Reviews (отзывы) — TestimonialConversation в публичном виде для мобайла
# ════════════════════════════════════════════════════════════════════
class ReviewListSerializer(serializers.ModelSerializer):
    """
    Соответствует мобильному `Review` interface.

    Сериализатор НЕ создаёт draft-полей (`has_draft`, `draft_text`,
    `draft_created_at`) пока их нет в модели. Возвращаем безопасные
    дефолты — мобайл это переварит.
    """
    branch_id     = serializers.SerializerMethodField()
    branch_name   = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    text          = serializers.SerializerMethodField()
    rating        = serializers.SerializerMethodField()
    source        = serializers.SerializerMethodField()
    sources       = serializers.SerializerMethodField()
    sentiment     = serializers.SerializerMethodField()
    has_draft       = serializers.SerializerMethodField()
    draft_text      = serializers.SerializerMethodField()
    draft_created_at = serializers.SerializerMethodField()
    review_link_yandex = serializers.SerializerMethodField()
    review_link_2gis   = serializers.SerializerMethodField()

    class Meta:
        model = TestimonialConversation
        fields = [
            'id',
            'source',
            'sources',
            'sentiment',
            'ai_comment',
            'branch_id',
            'branch_name',
            'customer_name',
            'vk_sender_id',
            'text',
            'rating',
            'last_message_at',
            'has_unread',
            'is_replied',
            'has_draft',
            'draft_text',
            'draft_created_at',
            'review_link_yandex',
            'review_link_2gis',
        ]

    def get_review_link_yandex(self, obj) -> str:
        # Ссылки точки; если кафе не определено (общий VK-отзыв) — фолбэк основной точки.
        if obj.branch_id and obj.branch.review_link_yandex:
            return obj.branch.review_link_yandex
        return self.context.get('fb_yandex', '')

    def get_review_link_2gis(self, obj) -> str:
        if obj.branch_id and obj.branch.review_link_2gis:
            return obj.branch.review_link_2gis
        return self.context.get('fb_2gis', '')

    def _last_message(self, obj):
        # Кэш на инстансе чтобы не дёргать БД повторно
        cached = getattr(obj, '_last_msg_cache', None)
        if cached is not None:
            return cached
        msg = (
            obj.messages.exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
            .order_by('-created_at').first()
            or obj.messages.order_by('-created_at').first()
        )
        obj._last_msg_cache = msg
        return msg

    def get_branch_id(self, obj) -> int | None:
        return obj.branch_id

    def get_branch_name(self, obj) -> str:
        return obj.branch.name if obj.branch_id else 'ВК группа'

    def get_customer_name(self, obj) -> str:
        # Зарегистрированный гость → имя из ClientBranch.client
        if obj.client_id and obj.client and obj.client.client:
            full = (f'{obj.client.client.first_name} {obj.client.client.last_name}').strip()
            return full or f'VK {obj.vk_sender_id}'
        # VK-гость
        if obj.vk_guest_id and obj.vk_guest:
            full = (f'{obj.vk_guest.first_name} {obj.vk_guest.last_name}').strip()
            return full or f'VK {obj.vk_sender_id}'
        return f'VK {obj.vk_sender_id}' if obj.vk_sender_id else 'Гость'

    def get_text(self, obj) -> str:
        msg = self._last_message(obj)
        return msg.text if msg else ''

    def get_rating(self, obj) -> int | None:
        msg = self._last_message(obj)
        return msg.rating if msg and msg.rating else None

    def _all_sources(self, obj) -> list[str]:
        """
        Все publi-источники сообщений (APP/VK_MESSAGE) этого треда + всех
        родственных тредов с тем же vk_sender_id (cross-conv unified thread).
        ADMIN_REPLY НЕ включаем — это ответ менеджера, не источник отзыва.
        """
        # Кэш на инстансе (вызывается из get_source И get_sources)
        cached = getattr(obj, '_sources_cache', None)
        if cached is not None:
            return cached

        qs = TestimonialMessage.objects.filter(conversation_id=obj.pk)
        if obj.vk_sender_id:
            # Подтягиваем все треды с тем же vk_sender_id (legacy branch=X +
            # новый branch=None + APP-conv того же гостя). Так бейдж в карточке
            # покажет реальный набор источников.
            qs = TestimonialMessage.objects.filter(
                conversation__vk_sender_id=obj.vk_sender_id,
            )
        srcs = set(qs.values_list('source', flat=True))
        srcs.discard(TestimonialMessage.Source.ADMIN_REPLY)
        # Стабильный порядок: APP перед VK_MESSAGE
        order = {'APP': 0, 'VK_MESSAGE': 1}
        result = sorted(srcs, key=lambda s: order.get(s, 99))
        obj._sources_cache = result
        return result

    def get_source(self, obj) -> str:
        """Главный источник для обратной совместимости: APP приоритетнее."""
        srcs = self._all_sources(obj)
        if 'APP' in srcs:
            return 'APP'
        return 'VK_MESSAGE'

    def get_sources(self, obj) -> list[str]:
        """Все источники: ['APP'] / ['VK_MESSAGE'] / ['APP', 'VK_MESSAGE']."""
        return self._all_sources(obj) or [self.get_source(obj)]

    def get_sentiment(self, obj) -> str:
        # Backend хранит 'WAITING' (default до AI-анализа), мобайл-тип ждёт 'PENDING'.
        # Маппим здесь, чтобы мобилка не падала на sentimentMeta(undefined).bg.
        # Тенанты с непроанализированными отзывами (свежие, либо AI-кредиты исчерпаны)
        # отдавали 'WAITING' и мобайл крашился на entry в экран Отзывов.
        v = obj.sentiment or ''
        return 'PENDING' if v == 'WAITING' else v

    def get_has_draft(self, obj) -> bool:
        # AI-черновики реализованы: показываем флаг если есть актуальный (не отвергнутый) черновик
        return bool(obj.ai_draft) and not obj.ai_draft_rejected

    def get_draft_text(self, obj) -> str | None:
        if obj.ai_draft and not obj.ai_draft_rejected:
            return obj.ai_draft
        return None

    def get_draft_created_at(self, obj):
        # Используем updated_at как proxy для времени генерации черновика
        if obj.ai_draft and not obj.ai_draft_rejected:
            return obj.updated_at.isoformat() if obj.updated_at else None
        return None


# ════════════════════════════════════════════════════════════════════
# Review messages
# ════════════════════════════════════════════════════════════════════
class ReviewMessageSerializer(serializers.ModelSerializer):
    admin_name  = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = TestimonialMessage
        fields = [
            'id', 'source', 'text', 'rating', 'created_at', 'admin_name', 'attachments',
            # LU-40: контекст «на что ответил гость» (текст+дата предыдущего
            # сообщения — обычно авто-опрос «Понравилось?»). Пустые если нет.
            'reply_to_text', 'reply_to_date',
        ]
        read_only_fields = fields

    def get_admin_name(self, obj):
        # У TestimonialMessage сейчас нет поля admin — отдаём None.
        # Когда добавится связь TestimonialMessage.admin = FK(User), сюда вернём имя.
        return None

    def get_attachments(self, obj):
        # [{'type','url','purged'}]. url доводим до абсолютного (для мобилки),
        # если есть request в контексте и url относительный (/media/...).
        req = self.context.get('request')
        out = []
        for a in obj.display_attachments():
            url = a['url']
            if url and req is not None and url.startswith('/'):
                url = req.build_absolute_uri(url)
            # RN Image (iOS ATS) НЕ грузит http:// — за nginx build_absolute_uri
            # отдаёт http (нет X-Forwarded-Proto). Принудительно https: media
            # отдаётся по https на всех тенант-доменах (тап в браузере и так открывал).
            if url and url.startswith('http://'):
                url = 'https://' + url[len('http://'):]
            out.append({'type': a['type'], 'url': url, 'purged': a['purged']})
        return out


# ════════════════════════════════════════════════════════════════════
# Reply
# ════════════════════════════════════════════════════════════════════
class ReviewReplySerializer(serializers.Serializer):
    text = serializers.CharField(required=True, allow_blank=False, max_length=4000)
