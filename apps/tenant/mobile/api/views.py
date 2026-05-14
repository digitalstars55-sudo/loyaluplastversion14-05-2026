"""
Мобильное API: list + reply для отзывов. Read+create only — не правит
ничего, кроме создания TestimonialMessage(source=ADMIN_REPLY) и flag'ов
`is_replied/has_unread` на TestimonialConversation.

Эти view не пересекаются с веб-views в analytics. Веб продолжает работать.
"""

from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenant.branch.models import (
    Branch,
    TestimonialConversation,
    TestimonialMessage,
)

from .serializers import (
    BranchSerializer,
    ReviewListSerializer,
    ReviewMessageSerializer,
    ReviewReplySerializer,
)


# ════════════════════════════════════════════════════════════════════
# Branches — мобайл-friendly список точек
# ════════════════════════════════════════════════════════════════════
class MobileBranchListAPIView(generics.ListAPIView):
    """GET /api/v1/mobile/branches/"""
    permission_classes = [IsAuthenticated]
    serializer_class = BranchSerializer

    def get_queryset(self):
        return Branch.objects.select_related('config').filter(is_active=True).order_by('name')


# ════════════════════════════════════════════════════════════════════
# Reviews
# ════════════════════════════════════════════════════════════════════
class MobileReviewListAPIView(generics.ListAPIView):
    """
    GET /api/v1/mobile/reviews/?branch_ids=1,2&period=30

    Возвращает список TestimonialConversation в формате `Review[]`
    как ожидает мобайл. period — целое число дней (фильтр по
    last_message_at).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ReviewListSerializer

    def get_queryset(self):
        qs = TestimonialConversation.objects.select_related(
            'branch', 'client__client', 'vk_guest',
        ).prefetch_related('messages').order_by('-last_message_at', '-id')

        # Фильтр по точкам
        branch_ids_raw = self.request.query_params.get('branch_ids')
        if branch_ids_raw:
            try:
                branch_ids = [int(x) for x in branch_ids_raw.split(',') if x.strip().isdigit()]
                if branch_ids:
                    qs = qs.filter(branch_id__in=branch_ids)
            except ValueError:
                pass

        # Фильтр по периоду
        period_raw = self.request.query_params.get('period')
        if period_raw:
            try:
                days = int(period_raw)
                if days > 0:
                    since = timezone.now() - timezone.timedelta(days=days)
                    qs = qs.filter(last_message_at__gte=since)
            except (ValueError, TypeError):
                pass

        return qs

    def list(self, request, *args, **kwargs):
        # Мобайл ожидает {reviews: [...]} а не голый массив
        qs = self.filter_queryset(self.get_queryset())
        ser = self.get_serializer(qs, many=True)
        return Response({'reviews': ser.data})


class MobileReviewMessagesAPIView(generics.ListAPIView):
    """GET /api/v1/mobile/reviews/{id}/messages/"""
    permission_classes = [IsAuthenticated]
    serializer_class = ReviewMessageSerializer

    def get_queryset(self):
        review_id = self.kwargs['review_id']
        return TestimonialMessage.objects.filter(
            conversation_id=review_id,
        ).order_by('created_at')

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        ser = self.get_serializer(qs, many=True)
        return Response({'messages': ser.data})


class MobileReviewReplyAPIView(APIView):
    """
    POST /api/v1/mobile/reviews/{review_id}/reply/  body: {text}

    Создаёт TestimonialMessage(source=ADMIN_REPLY) и обновляет
    conversation.is_replied=True, has_unread=False.
    Не отправляет сообщение во внешний VK — это делает существующая
    Celery-задача после сохранения (если такая есть в проекте).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, review_id: int):
        ser = ReviewReplySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data['text'].strip()

        conv = get_object_or_404(TestimonialConversation, pk=review_id)
        with transaction.atomic():
            msg = TestimonialMessage.objects.create(
                conversation=conv,
                source=TestimonialMessage.Source.ADMIN_REPLY,
                text=text,
            )
            conv.is_replied = True
            conv.has_unread = False
            conv.last_message_at = msg.created_at
            conv.save(update_fields=['is_replied', 'has_unread', 'last_message_at'])

        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'REVIEW_REPLY',
            target_type='review', target_id=conv.pk,
            target_label=str(conv)[:255],
            details=text[:500],
        )
        return Response(ReviewMessageSerializer(msg).data, status=status.HTTP_201_CREATED)


class MobileReviewResolveAPIView(APIView):
    """
    POST /api/v1/mobile/reviews/{review_id}/resolve/

    Помечает обращение как прочитанное (has_unread=False).
    Сообщение в VK не отправляется. Не разрешено для негативных VK-отзывов
    без ответа — мобайл это валидирует, дублирование тут.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, review_id: int):
        conv = get_object_or_404(TestimonialConversation, pk=review_id)
        # Защита: для VK-негатива без ответа не разрешаем закрывать без reply
        is_vk = not conv.messages.filter(source=TestimonialMessage.Source.APP).exists()
        if is_vk and conv.sentiment in ('NEGATIVE', 'PARTIALLY_NEGATIVE') and not conv.is_replied:
            return Response(
                {'detail': 'Нельзя закрыть негативный VK-отзыв без ответа гостю.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        conv.has_unread = False
        conv.save(update_fields=['has_unread'])
        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'REVIEW_RESOLVE',
            target_type='review', target_id=conv.pk,
            target_label=str(conv)[:255],
        )
        return Response({'ok': True})


# ════════════════════════════════════════════════════════════════════
# Guests — birthdays
# ════════════════════════════════════════════════════════════════════
class GuestBirthdaysAPIView(APIView):
    """
    GET /api/v1/guests/birthdays/?days_ahead=30&include_past=1

    Возвращает список гостей с ближайшими/прошедшими днями рождения.

    Параметры:
      days_ahead   — горизонт вперёд в днях (0..365, по умолчанию 30)
      include_past — 0|1 (по умолчанию 1) — включать ли уже прошедшие ДР этого года

    Группировка по уникальному vk_id (один гость может быть в нескольких точках —
    берём самый свежий ClientBranch). Сотрудники (is_employee=True) исключены.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date
        from django.db.models import Q, Sum
        from apps.tenant.branch.models import ClientBranch

        try:
            days_ahead = int(request.query_params.get('days_ahead', 30))
        except (TypeError, ValueError):
            days_ahead = 30
        days_ahead = max(0, min(days_ahead, 365))

        ip_raw = (request.query_params.get('include_past') or '1').lower()
        include_past = ip_raw in ('1', 'true', 'yes')
        past_window = -30 if include_past else 0

        today = date.today()
        this_year = today.year

        cbs = (
            ClientBranch.objects
            .filter(birth_date__isnull=False, is_employee=False)
            .select_related('client', 'branch')
            .order_by('client_id', '-created_at')
        )

        # Соберём один CB на client_id (самый свежий — он первый в order_by).
        chosen: dict[int, ClientBranch] = {}
        for cb in cbs.iterator():
            if cb.client_id not in chosen:
                chosen[cb.client_id] = cb

        # Посчитаем баланс монет в одной агрегации по всем CB каждого гостя.
        client_ids = list(chosen.keys())
        coin_rows = (
            ClientBranch.objects
            .filter(client_id__in=client_ids)
            .values('client_id')
            .annotate(
                income=Sum('transactions__amount', filter=Q(transactions__type='income')),
                expense=Sum('transactions__amount', filter=Q(transactions__type='expense')),
            )
        )
        coin_map = {r['client_id']: (r['income'] or 0) - (r['expense'] or 0) for r in coin_rows}

        # Логи birthday-рассылок этого года — для greeting_status.
        from apps.tenant.senler.models import AutoBroadcastLog
        vk_id_to_client = {cb.client.vk_id: cb.client_id for cb in chosen.values()}
        sent_logs = AutoBroadcastLog.objects.filter(
            trigger_type__in=('birthday', 'birthday_1d', 'birthday_7d'),
            vk_id__in=list(vk_id_to_client.keys()),
            sent_at__year=this_year,
        ).values_list('vk_id', flat=True)
        sent_vk_ids = set(sent_logs)

        birthdays = []
        for client_id, cb in chosen.items():
            bd = cb.birth_date
            try:
                bd_this = bd.replace(year=this_year)
            except ValueError:
                bd_this = bd.replace(year=this_year, month=2, day=28)

            days_until = (bd_this - today).days
            if days_until < past_window or days_until > days_ahead:
                continue

            # greeting_status: sent | planned | none (came пока не определяем — нужен activated birthday-prize)
            if cb.client.vk_id in sent_vk_ids:
                status_label = 'sent'
            elif days_until >= 0:
                status_label = 'planned'
            else:
                status_label = 'none'

            age_turning = (this_year - bd.year) if bd.year and bd.year > 1900 else None

            birthdays.append({
                'vk_id':              str(cb.client.vk_id),
                'first_name':         cb.client.first_name or '',
                'last_name':          cb.client.last_name or '',
                'phone':              '',
                'branch_name':        cb.branch.name if cb.branch_id else '',
                'coins':              coin_map.get(client_id, 0),
                'segment_emoji':      '',
                'segment_name':       '',
                'birthday':           bd.isoformat(),
                'birthday_this_year': bd_this.isoformat(),
                'days_until':         days_until,
                'age_turning':        age_turning,
                'is_loyal':           False,
                'greeting_status':    status_label,
            })

        birthdays.sort(key=lambda x: x['days_until'])
        return Response({'birthdays': birthdays})


# ════════════════════════════════════════════════════════════════════
# Daily codes — read-only list + manual generation
# ════════════════════════════════════════════════════════════════════
# Маппинг между мобильными покупателями (BIRTHDAY|SUPERPRIZE|OTHER)
# и серверными значениями DailyCodePurpose (birthday|game|quest).
_MOBILE_TO_BACKEND_PURPOSE = {
    'BIRTHDAY':   'birthday',
    'SUPERPRIZE': 'game',     # game-код требуется при подтверждении суперприза
    'OTHER':      'quest',
}
_BACKEND_TO_MOBILE_PURPOSE = {v: k for k, v in _MOBILE_TO_BACKEND_PURPOSE.items()}


def _serialize_daily_code(dc) -> dict:
    """Преобразовать DailyCode в формат, ожидаемый мобильным приложением."""
    return {
        'id':           dc.pk,
        'branch_id':    dc.branch_id,
        'branch_name':  dc.branch.name if dc.branch_id else '',
        'purpose':      _BACKEND_TO_MOBILE_PURPOSE.get(dc.purpose, 'OTHER'),
        'code':         dc.code,
        'valid_date':   dc.valid_date.isoformat(),
        'generated_by': (dc.generated_by or 'auto').upper(),
        'created_at':   dc.created_at.isoformat(),
    }


class DailyCodesListAPIView(APIView):
    """
    GET /api/v1/branch/daily-codes/

    Возвращает коды дня за последние 7 дней по всем активным точкам.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import timedelta
        from django.utils import timezone
        from apps.tenant.branch.models import DailyCode

        since = timezone.localdate() - timedelta(days=7)
        qs = (
            DailyCode.objects
            .filter(valid_date__gte=since, branch__is_active=True)
            .select_related('branch')
            .order_by('-valid_date', 'branch__name', 'purpose')
        )
        return Response({'codes': [_serialize_daily_code(dc) for dc in qs]})


class GenerateDailyCodeAPIView(APIView):
    """
    POST /api/v1/branch/daily-codes/generate/

    Body (JSON):
      branch_id — int, обязательно
      purpose   — 'BIRTHDAY' | 'SUPERPRIZE' | 'OTHER' (по умолчанию BIRTHDAY)

    Ручной/экстренный триггер: создаёт или перегенерирует 5-значный код
    для (branch, purpose, today). Возвращает обновлённую запись.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import random
        from django.utils import timezone
        from apps.tenant.branch.models import Branch, DailyCode

        try:
            branch_id = int(request.data.get('branch_id'))
        except (TypeError, ValueError):
            return Response(
                {'error': 'branch_id обязателен и должен быть числом'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        purpose_mob = (request.data.get('purpose') or 'BIRTHDAY').upper()
        purpose = _MOBILE_TO_BACKEND_PURPOSE.get(purpose_mob)
        if purpose is None:
            return Response(
                {'error': f'purpose: допустимы {list(_MOBILE_TO_BACKEND_PURPOSE.keys())}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            branch = Branch.objects.get(pk=branch_id)
        except Branch.DoesNotExist:
            return Response(
                {'error': 'Точка не найдена'},
                status=status.HTTP_404_NOT_FOUND,
            )

        today = timezone.localdate()
        new_code = f'{random.randint(0, 99999):05d}'
        dc, _ = DailyCode.objects.update_or_create(
            branch=branch,
            purpose=purpose,
            valid_date=today,
            defaults={'code': new_code, 'generated_by': 'manual'},
        )
        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'DAILY_CODE_MANUAL',
            target_type='daily_code',
            target_id=dc.pk,
            target_label=f'{branch.name} · {purpose_mob} · {today.isoformat()}',
            details=f'код: {dc.code}',
        )
        return Response(_serialize_daily_code(dc))


# ════════════════════════════════════════════════════════════════════
# AI drafts for reviews — regenerate / reject
# ════════════════════════════════════════════════════════════════════
def _build_draft_prompt(conv: TestimonialConversation) -> tuple[str, str]:
    """Возвращает (system_prompt, user_message) для генерации AI-черновика."""
    from django.db import connection
    from apps.tenant.branch.models import ReviewAutoReplyConfig

    tone = ReviewAutoReplyConfig.get_singleton().ai_tone
    tone_human = {
        'formal':   'официальный, вежливый',
        'friendly': 'дружелюбный, тёплый',
        'neutral':  'нейтральный, профессиональный',
    }.get(tone, 'дружелюбный')

    company_name = getattr(connection.tenant, 'name', 'наше заведение')
    sentiment_human = conv.get_sentiment_display() if conv.sentiment else 'не определён'

    # Соберём тред: первый APP-message и все предыдущие сообщения
    msgs = list(conv.messages.order_by('created_at').values('source', 'text'))
    thread_text = '\n'.join(
        f"[{m['source']}] {m['text']}" for m in msgs if (m.get('text') or '').strip()
    )

    system_prompt = (
        'Ты — менеджер заведения, отвечающий на отзывы гостей.\n'
        'Правила:\n'
        f'- Пиши на русском, тон: {tone_human}.\n'
        '- Коротко (2-4 предложения), без воды.\n'
        '- Не используй markdown, HTML, эмодзи кроме одного по необходимости.\n'
        '- Обращайся на "Вы".\n'
        '- Если отзыв негативный — извинись, не оправдывайся, предложи решение.\n'
        '- Если позитивный — поблагодари искренне, без шаблонов.\n'
        '- Не упоминай скидки/компенсации, если не сказано.\n'
        '- Верни ТОЛЬКО текст ответа, без пояснений и подписи.'
    )
    user_message = (
        f'Заведение: {company_name}\n'
        f'Тональность отзыва (определена ИИ): {sentiment_human}\n\n'
        f'Тред переписки:\n{thread_text}\n\n'
        f'Напиши черновик ответа от имени заведения.'
    )
    return system_prompt, user_message


def _call_claude_for_draft(conv: TestimonialConversation) -> tuple[str, int]:
    """Вызов Claude. Возвращает (text, http_status_code)."""
    import os
    from django.conf import settings as _settings

    api_key = getattr(_settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        return ('ANTHROPIC_API_KEY не настроен', 500)

    try:
        import anthropic
    except ImportError:
        return ('Библиотека anthropic не установлена', 500)

    proxy_url = os.getenv('AI_PROXY_URL', '')
    client = (
        anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
        if proxy_url else anthropic.Anthropic(api_key=api_key)
    )
    system_prompt, user_message = _build_draft_prompt(conv)
    try:
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=512,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_message}],
        )
        return (message.content[0].text.strip(), 200)
    except anthropic.BadRequestError as e:
        return (f'AI-сервис недоступен: {e}', 503)
    except Exception as e:
        return (f'Ошибка AI: {e}', 500)


class RegenerateReviewDraftAPIView(APIView):
    """
    POST /api/v1/analytics/reviews/{review_id}/regenerate-draft/

    Генерирует новый AI-черновик ответа для отзыва. Сохраняет в
    TestimonialConversation.ai_draft и сбрасывает ai_draft_rejected=False.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, review_id: int):
        conv = get_object_or_404(TestimonialConversation, pk=review_id)
        text, code = _call_claude_for_draft(conv)
        if code != 200:
            return Response({'error': text}, status=code)

        conv.ai_draft = text
        conv.ai_draft_rejected = False
        conv.save(update_fields=['ai_draft', 'ai_draft_rejected', 'updated_at'])
        return Response({'draft_text': text})


class RejectReviewDraftAPIView(APIView):
    """
    POST /api/v1/analytics/reviews/{review_id}/reject-draft/

    Помечает текущий AI-черновик как отклонённый админом. Сам черновик
    остаётся в БД (для аудита), но мобайл больше не показывает его до
    регенерации.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, review_id: int):
        conv = get_object_or_404(TestimonialConversation, pk=review_id)
        if not conv.ai_draft:
            return Response(
                {'error': 'Черновик отсутствует — нечего отклонять.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        conv.ai_draft_rejected = True
        conv.save(update_fields=['ai_draft_rejected', 'updated_at'])
        return Response({'ok': True})


# ════════════════════════════════════════════════════════════════════
# Global search across guests / reviews / products / quests / promotions
# ════════════════════════════════════════════════════════════════════
class GlobalSearchAPIView(APIView):
    """
    GET /api/v1/search/?q=...

    Поиск по 5 типам сущностей. Минимум 2 символа в запросе.
    Результаты ограничены 20 на каждый тип.
    """
    permission_classes = [IsAuthenticated]
    LIMIT = 20

    def get(self, request):
        from django.db.models import Q
        from apps.shared.guest.models import Client
        from apps.tenant.branch.models import (
            TestimonialMessage, Promotions,
        )
        from apps.tenant.catalog.models import Product
        from apps.tenant.quest.models import Quest

        q = (request.query_params.get('q') or '').strip()
        empty = {
            'q': q, 'total': 0,
            'guests': [], 'reviews': [], 'products': [], 'quests': [], 'promotions': [],
        }
        if len(q) < 2:
            return Response(empty)

        # ── Guests: имя/фамилия/vk_id ──────────────────────────────────────
        client_filter = Q(first_name__icontains=q) | Q(last_name__icontains=q)
        if q.isdigit():
            client_filter |= Q(vk_id=int(q))
        clients = (
            Client.objects.filter(client_filter, branch_profiles__isnull=False)
            .distinct()[: self.LIMIT]
        )
        guests = [
            {
                'type':     'guest',
                'id':       c.pk,
                'title':    (f'{c.first_name} {c.last_name}'.strip() or f'vk{c.vk_id}'),
                'subtitle': f'VK {c.vk_id}',
                'match':    '',
                'raw':      {
                    'vk_id':      str(c.vk_id),
                    'first_name': c.first_name,
                    'last_name':  c.last_name,
                },
            }
            for c in clients
        ]

        # ── Reviews: текст сообщения ───────────────────────────────────────
        rev_msgs = (
            TestimonialMessage.objects
            .filter(text__icontains=q)
            .select_related('conversation', 'conversation__branch')
            .order_by('-created_at')[: self.LIMIT * 2]
        )
        seen: set[int] = set()
        reviews: list[dict] = []
        for m in rev_msgs:
            cid = m.conversation_id
            if cid in seen or len(reviews) >= self.LIMIT:
                continue
            seen.add(cid)
            snippet = (m.text or '')[:120]
            reviews.append({
                'type':     'review',
                'id':       cid,
                'title':    snippet,
                'subtitle': (
                    m.conversation.branch.name
                    if m.conversation and m.conversation.branch_id else ''
                ),
                'match':    snippet,
                'raw':      {'id': cid, 'sentiment': getattr(m.conversation, 'sentiment', '')},
            })

        # ── Products ───────────────────────────────────────────────────────
        prods = Product.objects.filter(
            Q(name__icontains=q) | Q(description__icontains=q),
        )[: self.LIMIT]
        products = [
            {
                'type':     'product',
                'id':       p.pk,
                'title':    p.name,
                'subtitle': f'{p.price} ★',
                'match':    '',
                'raw':      {'id': p.pk, 'name': p.name, 'price': p.price},
            }
            for p in prods
        ]

        # ── Quests ─────────────────────────────────────────────────────────
        qsts = Quest.objects.filter(
            Q(name__icontains=q) | Q(description__icontains=q),
        ).select_related('branch')[: self.LIMIT]
        quests = [
            {
                'type':     'quest',
                'id':       qst.pk,
                'title':    qst.name,
                'subtitle': f'+{qst.reward} ★ · {qst.branch.name if qst.branch_id else ""}',
                'match':    '',
                'raw':      {'id': qst.pk, 'name': qst.name, 'reward': qst.reward},
            }
            for qst in qsts
        ]

        # ── Promotions ─────────────────────────────────────────────────────
        promos = Promotions.objects.filter(
            Q(title__icontains=q) | Q(discount__icontains=q),
        ).select_related('branch')[: self.LIMIT]
        promotions = [
            {
                'type':     'promotion',
                'id':       pr.pk,
                'title':    pr.title,
                'subtitle': (pr.discount or '')[:80],
                'match':    '',
                'raw':      {'id': pr.pk, 'title': pr.title, 'discount': pr.discount},
            }
            for pr in promos
        ]

        total = len(guests) + len(reviews) + len(products) + len(quests) + len(promotions)
        return Response({
            'q':          q,
            'total':      total,
            'guests':     guests,
            'reviews':    reviews,
            'products':   products,
            'quests':     quests,
            'promotions': promotions,
        })


# ════════════════════════════════════════════════════════════════════
# Audit log
# ════════════════════════════════════════════════════════════════════
class AuditLogAPIView(APIView):
    """
    GET /api/v1/audit-log/?staff_id=&action_type=&limit=

    Журнал действий, выполненных через мобильное API.
    Фильтры опциональны. Limit по умолчанию 50, максимум 200.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.tenant.branch.models import AuditLog

        try:
            limit = int(request.query_params.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))

        qs = AuditLog.objects.order_by('-created_at')

        staff_id = request.query_params.get('staff_id') or ''
        if staff_id and staff_id.isdigit():
            qs = qs.filter(staff_id=int(staff_id))

        action = request.query_params.get('action_type') or ''
        if action:
            qs = qs.filter(action_type=action)

        entries = []
        for log in qs[:limit]:
            entries.append({
                'id':           log.pk,
                'staff_id':     log.staff_id or 0,
                'staff_name':   log.staff_name or '',
                'action_type':  log.action_type,
                'target_type':  log.target_type or None,
                'target_id':    log.target_id or None,
                'target_label': log.target_label or None,
                'details':      log.details or None,
                'delta':        log.delta or None,
                'created_at':   log.created_at.isoformat(),
            })
        return Response({'entries': entries})


# ════════════════════════════════════════════════════════════════════
# Subscription / billing status
# ════════════════════════════════════════════════════════════════════
class SubscriptionStatusAPIView(APIView):
    """
    GET /api/v1/billing/status/

    Статус подписки текущего тенанта. Plan/price хардкод (в Company-модели
    их нет), paid_until берём из Company.paid_until.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db import connection
        from apps.shared.clients.models import Company

        tenant = getattr(connection, 'tenant', None)
        paid_until = getattr(tenant, 'paid_until', None) if tenant else None
        plan_code = getattr(tenant, 'plan_code', None) or 'standard'
        price_rub = getattr(tenant, 'plan_price_rub', None) or 4900
        auto_pay = bool(getattr(tenant, 'auto_pay_enabled', False))
        try:
            plan_label = Company.Plan(plan_code).label
        except ValueError:
            plan_label = 'Стандарт'

        paid_iso = paid_until.isoformat() if paid_until else ''
        return Response({
            'plan':             plan_code,
            'plan_label':       plan_label,
            'price_rub':        price_rub,
            'next_payment_at':  paid_iso,
            'auto_pay_enabled': auto_pay,
            'paid_until':       paid_iso,
        })


# ════════════════════════════════════════════════════════════════════
# Staff — list + update role/active
# ════════════════════════════════════════════════════════════════════
_BACKEND_ROLE_TO_MOBILE = {
    'superadmin':    'owner',
    'network_admin': 'manager',
    'client':        'viewer',
}
_MOBILE_ROLE_TO_BACKEND = {v: k for k, v in _BACKEND_ROLE_TO_MOBILE.items()}
_ROLE_LABELS = {'owner': 'Владелец', 'manager': 'Управляющий', 'viewer': 'Просмотр'}

_PERMS_KEYS = (
    'see_analytics', 'see_reviews', 'see_broadcasts', 'see_guests', 'see_branches',
    'edit_thresholds', 'reply_reviews', 'send_broadcasts', 'manage_staff', 'edit_profile',
    'manage_catalog', 'manage_quests', 'manage_promotions', 'adjust_coins',
)


def _perms_for_role(mobile_role: str) -> dict:
    """Дефолт-карта прав по роли — используется при создании StaffProfile."""
    if mobile_role == 'owner':
        return {k: True for k in _PERMS_KEYS}
    if mobile_role == 'manager':
        return {k: (k != 'manage_staff') for k in _PERMS_KEYS}
    # viewer
    return {k: k.startswith('see_') or k == 'edit_profile' for k in _PERMS_KEYS}


def _get_or_create_staff_profile(user):
    """Лениво создаёт StaffProfile с дефолтными правами по текущей роли."""
    from apps.tenant.branch.models import StaffProfile
    profile, created = StaffProfile.objects.get_or_create(user_id=user.pk)
    if created or not profile.permissions:
        mob_role = _BACKEND_ROLE_TO_MOBILE.get(user.role, 'viewer')
        profile.permissions = _perms_for_role(mob_role)
        profile.save(update_fields=['permissions'])
    return profile


def _merge_permissions(stored: dict, mobile_role: str) -> dict:
    """Заполняет недостающие ключи дефолтами по роли — гарантия 14-полей."""
    defaults = _perms_for_role(mobile_role)
    merged = {**defaults, **(stored or {})}
    # Удалим возможные неизвестные ключи, чтобы не утекали в API
    return {k: bool(merged.get(k, defaults[k])) for k in _PERMS_KEYS}


def _serialize_staff(user) -> dict:
    profile = _get_or_create_staff_profile(user)
    mob_role = _BACKEND_ROLE_TO_MOBILE.get(user.role, 'viewer')
    return {
        'id':            user.pk,
        'full_name':     user.get_full_name() or user.username,
        'role':          mob_role,
        'role_label':    _ROLE_LABELS.get(mob_role, '—'),
        'email':         user.email or '',
        'phone':         profile.phone or '',
        'active':        bool(user.is_active),
        'permissions':   _merge_permissions(profile.permissions, mob_role),
        'branch_ids':    list(profile.branch_access.values_list('pk', flat=True)),
        'invited_at':    user.date_joined.isoformat() if user.date_joined else '',
        'last_active_at': (profile.last_active_at or user.last_login).isoformat()
                          if (profile.last_active_at or user.last_login) else None,
    }


class StaffListAPIView(APIView):
    """
    GET /api/v1/staff/

    Список пользователей с ролями superadmin/network_admin/client, у которых
    есть доступ к текущему тенанту (или они супер-админы — у них доступ ко всем).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.contrib.auth import get_user_model
        from django.db.models import Q
        from django.db import connection

        User = get_user_model()
        tenant = getattr(connection, 'tenant', None)
        qs = User.objects.filter(role__in=('superadmin', 'network_admin', 'client'))
        if tenant is not None and getattr(tenant, 'pk', None):
            # superadmin или явно привязанные к этой компании
            qs = qs.filter(Q(role='superadmin') | Q(companies=tenant)).distinct()
        qs = qs.order_by('pk')
        return Response({'staff': [_serialize_staff(u) for u in qs]})


class StaffDetailAPIView(APIView):
    """
    PATCH /api/v1/staff/{id}/

    Обновляет роль и/или активность сотрудника. На бэке хранится только
    role + is_active; per-action permissions, branch_ids — поля мобильного
    клиента, не персистятся.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, staff_id: int):
        from django.contrib.auth import get_user_model
        from apps.tenant.branch.models import Branch
        User = get_user_model()
        try:
            user = User.objects.get(pk=staff_id)
        except User.DoesNotExist:
            return Response({'error': 'Сотрудник не найден'}, status=status.HTTP_404_NOT_FOUND)

        profile = _get_or_create_staff_profile(user)

        changed_user_fields: list[str] = []
        changed_profile_fields: list[str] = []
        before = {
            'role': user.role,
            'is_active': user.is_active,
            'permissions': dict(profile.permissions or {}),
            'phone': profile.phone,
            'branch_ids': list(profile.branch_access.values_list('pk', flat=True)),
        }

        mob_role = request.data.get('role')
        if mob_role is not None:
            backend_role = _MOBILE_ROLE_TO_BACKEND.get(mob_role)
            if backend_role is None:
                return Response(
                    {'error': f'role: допустимы {list(_MOBILE_ROLE_TO_BACKEND.keys())}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if user.role != backend_role:
                user.role = backend_role
                changed_user_fields.append('role')
                # При смене роли — сбрасываем permissions на дефолты этой роли
                profile.permissions = _perms_for_role(mob_role)
                changed_profile_fields.append('permissions')

        active = request.data.get('active')
        if active is not None:
            new_active = bool(active)
            if user.is_active != new_active:
                user.is_active = new_active
                changed_user_fields.append('is_active')

        perms = request.data.get('permissions')
        if perms is not None:
            if not isinstance(perms, dict):
                return Response({'error': 'permissions должен быть объектом'},
                                status=status.HTTP_400_BAD_REQUEST)
            merged = {k: bool(perms.get(k, profile.permissions.get(k, False)))
                      for k in _PERMS_KEYS}
            profile.permissions = merged
            if 'permissions' not in changed_profile_fields:
                changed_profile_fields.append('permissions')

        phone = request.data.get('phone')
        if phone is not None:
            new_phone = str(phone).strip()[:32]
            if profile.phone != new_phone:
                profile.phone = new_phone
                changed_profile_fields.append('phone')

        branch_ids = request.data.get('branch_ids')
        m2m_changed = False
        if branch_ids is not None:
            if not isinstance(branch_ids, list):
                return Response({'error': 'branch_ids должен быть списком'},
                                status=status.HTTP_400_BAD_REQUEST)
            try:
                ids = [int(x) for x in branch_ids]
            except (TypeError, ValueError):
                return Response({'error': 'branch_ids должен содержать числа'},
                                status=status.HTTP_400_BAD_REQUEST)
            existing = set(Branch.objects.filter(pk__in=ids).values_list('pk', flat=True))
            profile.branch_access.set(existing)
            m2m_changed = True

        if changed_user_fields:
            user.save(update_fields=changed_user_fields)
        if changed_profile_fields:
            profile.save(update_fields=changed_profile_fields + ['updated_at'])

        if changed_user_fields or changed_profile_fields or m2m_changed:
            from apps.tenant.branch.audit import log_audit
            if 'role' in changed_user_fields or 'permissions' in changed_profile_fields or m2m_changed:
                action = 'STAFF_PERMS'
            else:
                action = 'STAFF_TOGGLE'
            log_audit(
                request.user, action,
                target_type='staff',
                target_id=user.pk,
                target_label=(user.get_full_name() or user.username)[:255],
                delta={
                    'before': before,
                    'after': {
                        'role': user.role,
                        'is_active': user.is_active,
                        'permissions': profile.permissions,
                        'phone': profile.phone,
                        'branch_ids': list(profile.branch_access.values_list('pk', flat=True)),
                    },
                },
            )

        return Response(_serialize_staff(user))


class StaffInviteAPIView(APIView):
    """
    POST /api/v1/staff/invite/

    Создаёт нового сотрудника текущего тенанта.

    Body (JSON):
      full_name, email, phone, role ('manager' | 'viewer'), branch_ids

    Возвращает Staff-объект + одноразовый password (его нужно передать
    приглашённому, чтобы он зашёл в первый раз и сменил его).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import secrets
        from django.contrib.auth import get_user_model
        from django.db import connection, transaction
        from apps.tenant.branch.models import Branch

        User = get_user_model()
        d = request.data or {}

        full_name = (d.get('full_name') or '').strip()
        if not full_name:
            return Response({'error': 'full_name обязателен'},
                            status=status.HTTP_400_BAD_REQUEST)

        email = (d.get('email') or '').strip().lower()
        phone = (d.get('phone') or '').strip()[:32]

        mob_role = (d.get('role') or 'viewer').lower()
        if mob_role not in ('manager', 'viewer'):
            return Response({'error': 'role: допустимы manager, viewer'},
                            status=status.HTTP_400_BAD_REQUEST)
        backend_role = _MOBILE_ROLE_TO_BACKEND[mob_role]

        # Генерим уникальный username
        base_user = (email.split('@')[0] if email else full_name).strip() or 'staff'
        base_user = ''.join(c for c in base_user.lower() if c.isalnum() or c in '._-')[:150] or 'staff'
        username = base_user
        suffix = 0
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f'{base_user}{suffix}'

        # Случайный пароль и токен приглашения
        password = secrets.token_urlsafe(12)
        invitation_token = secrets.token_urlsafe(24)

        # Привязка к точкам
        raw_branch_ids = d.get('branch_ids') or []
        if not isinstance(raw_branch_ids, list):
            return Response({'error': 'branch_ids должен быть списком'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            branch_ids = [int(x) for x in raw_branch_ids]
        except (TypeError, ValueError):
            return Response({'error': 'branch_ids должен содержать числа'},
                            status=status.HTTP_400_BAD_REQUEST)
        valid_branch_ids = list(
            Branch.objects.filter(pk__in=branch_ids).values_list('pk', flat=True)
        )

        # Имя/фамилия из full_name (best-effort)
        parts = full_name.split(' ', 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ''

        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=first_name[:150],
                last_name=last_name[:150],
            )
            user.role = backend_role
            user.is_active = True
            # Привязка к текущему тенанту
            tenant = getattr(connection, 'tenant', None)
            if tenant is not None and getattr(tenant, 'pk', None):
                user.save()
                user.companies.add(tenant)
            else:
                user.save()

            from apps.tenant.branch.models import StaffProfile
            profile = StaffProfile.objects.create(
                user_id=user.pk,
                phone=phone,
                permissions=_perms_for_role(mob_role),
                invitation_token=invitation_token,
            )
            if valid_branch_ids:
                profile.branch_access.set(valid_branch_ids)

        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'STAFF_INVITE',
            target_type='staff',
            target_id=user.pk,
            target_label=full_name[:255],
            details=f'username={username}, role={mob_role}, branches={valid_branch_ids}',
        )

        body = _serialize_staff(user)
        # Одноразовая выдача учётки — мобайл показывает админу.
        body['temp_password'] = password
        body['invitation_token'] = invitation_token
        return Response(body, status=status.HTTP_201_CREATED)


# ════════════════════════════════════════════════════════════════════
# Catalog: ProductCategory CRUD
# ════════════════════════════════════════════════════════════════════
def _serialize_category(c) -> dict:
    return {
        'id':             c.pk,
        'branch_id':      c.branch_id,
        'name':           c.name,
        'ordering':       c.ordering,
        'products_count': getattr(c, 'products_count', None) or c.product_assignments.count(),
    }


class ProductCategoryListCreateAPIView(APIView):
    """GET /api/v1/catalog/categories/?branch_ids= ; POST same URL."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count
        from apps.tenant.catalog.models import ProductCategory

        qs = ProductCategory.objects.annotate(products_count=Count('product_assignments'))
        branch_ids_raw = request.query_params.get('branch_ids') or ''
        if branch_ids_raw:
            try:
                ids = [int(x) for x in branch_ids_raw.split(',') if x.strip()]
                qs = qs.filter(branch_id__in=ids)
            except ValueError:
                return Response({'error': 'branch_ids: должны быть числами'}, status=400)
        return Response({'categories': [_serialize_category(c) for c in qs]})

    def post(self, request):
        from apps.tenant.catalog.models import ProductCategory
        from apps.tenant.branch.models import Branch
        try:
            branch_id = int(request.data.get('branch_id'))
        except (TypeError, ValueError):
            return Response({'error': 'branch_id обязателен'}, status=400)
        if not Branch.objects.filter(pk=branch_id).exists():
            return Response({'error': 'Точка не найдена'}, status=404)
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'name обязателен'}, status=400)
        c = ProductCategory.objects.create(
            branch_id=branch_id,
            name=name[:255],
            ordering=int(request.data.get('ordering') or 0),
        )
        return Response(_serialize_category(c), status=201)


class ProductCategoryDetailAPIView(APIView):
    """PATCH/DELETE /api/v1/catalog/categories/<id>/."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk: int):
        from apps.tenant.catalog.models import ProductCategory
        c = get_object_or_404(ProductCategory, pk=pk)
        if 'name' in request.data:
            c.name = (request.data['name'] or c.name)[:255]
        if 'ordering' in request.data:
            try:
                c.ordering = int(request.data['ordering'])
            except (TypeError, ValueError):
                return Response({'error': 'ordering должен быть числом'}, status=400)
        c.save()
        return Response(_serialize_category(c))

    def delete(self, request, pk: int):
        from apps.tenant.catalog.models import ProductCategory
        c = get_object_or_404(ProductCategory, pk=pk)
        c.delete()
        return Response(status=204)


# ════════════════════════════════════════════════════════════════════
# Catalog: Product CRUD
# ════════════════════════════════════════════════════════════════════
def _serialize_assignment(pb) -> dict:
    return {
        'branch_id':   pb.branch_id,
        'category_id': pb.category_id,
        'ordering':    pb.ordering,
        'is_visible':  pb.is_active,
    }


def _serialize_product(p) -> dict:
    image_url = None
    if p.image:
        try:
            image_url = p.image.url
        except Exception:
            image_url = None
    return {
        'id':                p.pk,
        'name':              p.name,
        'description':       p.description or '',
        'emoji':             p.emoji or '',
        'image_url':         image_url,
        'price':             p.price,
        'is_super_prize':    p.is_super_prize,
        'is_birthday_prize': p.is_birthday_prize,
        'assignments':       [_serialize_assignment(pb) for pb in p.branch_assignments.all()],
        'created_at':        p.created_at.isoformat(),
        'updated_at':        p.updated_at.isoformat(),
    }


def _apply_assignments(product, raw):
    """raw: list[{branch_id, category_id, ordering, is_visible}] или JSON-строка."""
    import json as _json
    from apps.tenant.catalog.models import ProductBranch
    from apps.tenant.branch.models import Branch

    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            return
    if not isinstance(raw, list):
        return

    seen_branch_ids: set[int] = set()
    for a in raw:
        try:
            bid = int(a.get('branch_id'))
        except (TypeError, ValueError):
            continue
        if not Branch.objects.filter(pk=bid).exists():
            continue
        cat_id = a.get('category_id')
        try:
            cat_id = int(cat_id) if cat_id else None
        except (TypeError, ValueError):
            cat_id = None
        ordering = int(a.get('ordering') or 0)
        is_visible = bool(a.get('is_visible', True))
        ProductBranch.objects.update_or_create(
            product=product, branch_id=bid,
            defaults={'category_id': cat_id, 'ordering': ordering, 'is_active': is_visible},
        )
        seen_branch_ids.add(bid)
    # Удаляем привязки к точкам, не упомянутым в новой версии
    product.branch_assignments.exclude(branch_id__in=seen_branch_ids).delete()


class ProductListCreateAPIView(APIView):
    """GET /api/v1/catalog/products/ ; POST (multipart) same URL."""
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        from apps.tenant.catalog.models import Product
        qs = Product.objects.prefetch_related('branch_assignments').order_by('name')
        return Response({'products': [_serialize_product(p) for p in qs]})

    def post(self, request):
        from apps.tenant.catalog.models import Product
        d = request.data
        name = (d.get('name') or '').strip()
        if not name:
            return Response({'error': 'name обязателен'}, status=400)
        try:
            price = int(d.get('price') or 0)
        except (TypeError, ValueError):
            return Response({'error': 'price должен быть числом'}, status=400)

        p = Product.objects.create(
            name=name[:255],
            description=(d.get('description') or '')[:2000],
            emoji=(d.get('emoji') or '')[:8],
            price=max(0, price),
            is_super_prize=str(d.get('is_super_prize', '')).lower() in ('1', 'true', 'yes'),
            is_birthday_prize=str(d.get('is_birthday_prize', '')).lower() in ('1', 'true', 'yes'),
        )
        if 'image' in request.FILES:
            p.image = request.FILES['image']
            p.save(update_fields=['image'])

        if 'assignments' in d:
            _apply_assignments(p, d.get('assignments'))

        return Response(_serialize_product(p), status=201)


class ProductDetailAPIView(APIView):
    """PATCH/DELETE /api/v1/catalog/products/<id>/."""
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def patch(self, request, pk: int):
        from apps.tenant.catalog.models import Product
        p = get_object_or_404(Product, pk=pk)
        d = request.data
        if 'name' in d:
            p.name = (d['name'] or p.name)[:255]
        if 'description' in d:
            p.description = (d['description'] or '')[:2000]
        if 'emoji' in d:
            p.emoji = (d['emoji'] or '')[:8]
        if 'price' in d:
            try:
                p.price = max(0, int(d['price']))
            except (TypeError, ValueError):
                return Response({'error': 'price должен быть числом'}, status=400)
        if 'is_super_prize' in d:
            p.is_super_prize = str(d['is_super_prize']).lower() in ('1', 'true', 'yes')
        if 'is_birthday_prize' in d:
            p.is_birthday_prize = str(d['is_birthday_prize']).lower() in ('1', 'true', 'yes')
        if 'image' in request.FILES:
            p.image = request.FILES['image']
        p.save()

        if 'assignments' in d:
            _apply_assignments(p, d.get('assignments'))

        return Response(_serialize_product(p))

    def delete(self, request, pk: int):
        from apps.tenant.catalog.models import Product
        p = get_object_or_404(Product, pk=pk)
        p.delete()
        return Response(status=204)


# ════════════════════════════════════════════════════════════════════
# Quests CRUD
# ════════════════════════════════════════════════════════════════════
def _serialize_quest(q) -> dict:
    return {
        'id':            q.pk,
        'branch_id':     q.branch_id,
        'name':          q.name,
        'description':   q.description or '',
        'reward':        q.reward,
        'is_active':     q.is_active,
        'ordering':      q.ordering,
        'submits_count': getattr(q, 'submits_count', None) or q.submits.count(),
        'created_at':    q.created_at.isoformat(),
        'updated_at':    q.updated_at.isoformat(),
    }


class QuestListCreateAPIView(APIView):
    """GET /api/v1/quests/ ; POST same URL."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count
        from apps.tenant.quest.models import Quest
        qs = Quest.objects.annotate(submits_count=Count('submits')).order_by('ordering', 'name')
        return Response({'quests': [_serialize_quest(q) for q in qs]})

    def post(self, request):
        from apps.tenant.quest.models import Quest
        from apps.tenant.branch.models import Branch
        d = request.data
        try:
            branch_id = int(d.get('branch_id'))
        except (TypeError, ValueError):
            return Response({'error': 'branch_id обязателен'}, status=400)
        if not Branch.objects.filter(pk=branch_id).exists():
            return Response({'error': 'Точка не найдена'}, status=404)
        name = (d.get('name') or '').strip()
        if not name:
            return Response({'error': 'name обязателен'}, status=400)
        try:
            reward = int(d.get('reward') or 0)
        except (TypeError, ValueError):
            return Response({'error': 'reward должен быть числом'}, status=400)

        q = Quest.objects.create(
            branch_id=branch_id,
            name=name[:255],
            description=(d.get('description') or '')[:2000],
            reward=max(0, reward),
            is_active=bool(d.get('is_active', True)),
            ordering=int(d.get('ordering') or 0),
        )
        return Response(_serialize_quest(q), status=201)


class QuestDetailAPIView(APIView):
    """PATCH/DELETE /api/v1/quests/<id>/."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk: int):
        from apps.tenant.quest.models import Quest
        q = get_object_or_404(Quest, pk=pk)
        d = request.data
        if 'name' in d:
            q.name = (d['name'] or q.name)[:255]
        if 'description' in d:
            q.description = (d['description'] or '')[:2000]
        if 'reward' in d:
            try:
                q.reward = max(0, int(d['reward']))
            except (TypeError, ValueError):
                return Response({'error': 'reward должен быть числом'}, status=400)
        if 'is_active' in d:
            q.is_active = bool(d['is_active'])
        if 'ordering' in d:
            try:
                q.ordering = int(d['ordering'])
            except (TypeError, ValueError):
                return Response({'error': 'ordering должен быть числом'}, status=400)
        q.save()
        return Response(_serialize_quest(q))

    def delete(self, request, pk: int):
        from apps.tenant.quest.models import Quest
        q = get_object_or_404(Quest, pk=pk)
        q.delete()
        return Response(status=204)


# ════════════════════════════════════════════════════════════════════
# Promotions CRUD
# ════════════════════════════════════════════════════════════════════
def _serialize_promotion(p) -> dict:
    image_url = None
    if p.images:
        try:
            image_url = p.images.url
        except Exception:
            image_url = None
    return {
        'id':          p.pk,
        'branch_id':   p.branch_id,
        'branch_name': p.branch.name if p.branch_id else '',
        'title':       p.title,
        'discount':    p.discount,
        'dates':       p.dates,
        'image_url':   image_url,
        'created_at':  p.created_at.isoformat(),
        'updated_at':  p.updated_at.isoformat(),
    }


class PromotionListCreateAPIView(APIView):
    """GET /api/v1/branch/promotions/ ; POST (multipart) same URL."""
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        from apps.tenant.branch.models import Promotions
        qs = Promotions.objects.select_related('branch').order_by('-created_at')
        return Response({'promotions': [_serialize_promotion(p) for p in qs]})

    def post(self, request):
        from apps.tenant.branch.models import Promotions, Branch
        d = request.data
        try:
            branch_id = int(d.get('branch_id'))
        except (TypeError, ValueError):
            return Response({'error': 'branch_id обязателен'}, status=400)
        if not Branch.objects.filter(pk=branch_id).exists():
            return Response({'error': 'Точка не найдена'}, status=404)
        title = (d.get('title') or '').strip()
        discount = (d.get('discount') or '').strip()
        dates = (d.get('dates') or '').strip()
        if not (title and discount and dates):
            return Response({'error': 'title, discount, dates обязательны'}, status=400)

        p = Promotions(
            branch_id=branch_id,
            title=title[:100],
            discount=discount[:500],
            dates=dates[:255],
        )
        if 'image' in request.FILES:
            p.images = request.FILES['image']
        p.save()
        return Response(_serialize_promotion(p), status=201)


class PromotionDetailAPIView(APIView):
    """PATCH/DELETE /api/v1/branch/promotions/<id>/."""
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def patch(self, request, pk: int):
        from apps.tenant.branch.models import Promotions
        p = get_object_or_404(Promotions, pk=pk)
        d = request.data
        if 'title' in d:
            p.title = (d['title'] or p.title)[:100]
        if 'discount' in d:
            p.discount = (d['discount'] or p.discount)[:500]
        if 'dates' in d:
            p.dates = (d['dates'] or p.dates)[:255]
        if 'image' in request.FILES:
            p.images = request.FILES['image']
        p.save()
        return Response(_serialize_promotion(p))

    def delete(self, request, pk: int):
        from apps.tenant.branch.models import Promotions
        p = get_object_or_404(Promotions, pk=pk)
        p.delete()
        return Response(status=204)


# ════════════════════════════════════════════════════════════════════
# Support chat (мобильное приложение ↔ менеджер LoyalUP)
# ════════════════════════════════════════════════════════════════════
_SUPPORT_MANAGER = {
    'id':          1,
    'name':        'Менеджер LoyalUP',
    'role':        'Ваш менеджер',
    'avatar_url':  '',
    'online':      True,
    'last_seen':   '',
    'phone':       '+74950000000',
    'work_hours':  'Пн–Пт 10:00–19:00',
}


def _serialize_support_message(m) -> dict:
    if m.read_at:
        status_label = 'read'
    else:
        status_label = 'delivered'
    return {
        'id':         m.pk,
        'sender':     m.sender,
        'text':       m.text or '',
        'created_at': m.created_at.isoformat(),
        'read_at':    m.read_at.isoformat() if m.read_at else None,
        'status':     status_label,
        'attachments': [],
    }


class SupportChatManagerAPIView(APIView):
    """GET /api/v1/support/chat/manager/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        info = dict(_SUPPORT_MANAGER)
        info['last_seen'] = timezone.now().isoformat()
        return Response(info)


class SupportChatMessagesAPIView(APIView):
    """
    GET  /api/v1/support/chat/messages/  — последние 200 сообщений
    POST /api/v1/support/chat/messages/  — отправить сообщение от пользователя
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        from apps.tenant.branch.models import SupportChatMessage

        qs = list(SupportChatMessage.objects.order_by('created_at')[:500])
        # Помечаем как прочитанные те manager-сообщения, которые ещё не отмечены —
        # вызывающий клиент (user) сейчас их видит.
        now = timezone.now()
        unread_manager = [m for m in qs if m.sender == SupportChatMessage.Sender.MANAGER and not m.read_at]
        if unread_manager:
            SupportChatMessage.objects.filter(
                pk__in=[m.pk for m in unread_manager]
            ).update(read_at=now)
            for m in unread_manager:
                m.read_at = now
        return Response({'messages': [_serialize_support_message(m) for m in qs]})

    def post(self, request):
        from apps.tenant.branch.models import SupportChatMessage

        text = (request.data.get('text') or '').strip()
        if not text:
            return Response({'error': 'text не может быть пустым'}, status=400)
        if len(text) > 4096:
            return Response({'error': 'text превышает 4096 символов'}, status=400)

        # Менеджер LoyalUP может писать от лица manager-стороны, если у него
        # is_superuser. Все остальные пишут как user.
        sender_param = (request.data.get('sender') or 'user').lower()
        is_super = bool(getattr(request.user, 'is_superuser', False))
        if sender_param == 'manager' and is_super:
            sender = SupportChatMessage.Sender.MANAGER
        else:
            sender = SupportChatMessage.Sender.USER

        m = SupportChatMessage.objects.create(
            sender=sender,
            author_id=request.user.pk if request.user.is_authenticated else None,
            text=text,
        )
        return Response(_serialize_support_message(m), status=201)
