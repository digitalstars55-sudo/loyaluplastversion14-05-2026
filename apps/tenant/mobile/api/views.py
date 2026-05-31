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
        conv = TestimonialConversation.objects.filter(pk=review_id).first()
        # Если это VK-гость — объединяем сообщения ВСЕХ его тредов по vk_sender_id.
        # Исторически диалог мог разъехаться (legacy branch=X + новый branch=None):
        # сообщения гостя в одном треде, ответы менеджера в другом. Показываем
        # полный диалог, чтобы в мобилке были видны и сообщения, и ответы.
        if conv and conv.vk_sender_id:
            return TestimonialMessage.objects.filter(
                conversation__vk_sender_id=conv.vk_sender_id,
            ).order_by('created_at')
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

    Для VK-диалога (есть vk_sender_id) РЕАЛЬНО отправляет сообщение гостю в
    ВКонтакте через send_vk_reply (он же сохранит ADMIN_REPLY с vk_message_id
    и обновит conv). Для APP-отзыва (нет VK-канала) — сохраняет ответ локально.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, review_id: int):
        ser = ReviewReplySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data['text'].strip()

        conv = get_object_or_404(TestimonialConversation, pk=review_id)

        delivered_to_vk = False
        vk_error: str | None = None
        if conv.vk_sender_id:
            # APP- ИЛИ VK-conv: оба имеют vk_sender_id, оба можно ответить
            # через сообщество (для APP-гостя это значит, что менеджер
            # пишет ему в ЛС от имени группы).
            # Если гость заблокировал сообщения от группы — VK вернёт error,
            # тогда сохраняем локально с warning (а не 502, чтобы UI показал
            # ответ менеджера в треде — гость хотя бы увидит при следующем
            # заходе через миниапп).
            from apps.tenant.branch.api.services import send_vk_reply
            try:
                msg = send_vk_reply(conv, text)
                delivered_to_vk = True
            except Exception as e:
                vk_error = str(e)
                msg = None

        if not delivered_to_vk:
            # Fallback: локальное сохранение (как для conv без vk_sender_id
            # ИЛИ когда VK reply упал — гость недоступен через ВК).
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
        # Возвращаем msg + флаг доставки в ВК, чтобы мобайл показал warning
        # «гость не получит уведомление в ВК» если delivered_to_vk=False.
        data = ReviewMessageSerializer(msg).data
        data['delivered_to_vk'] = delivered_to_vk
        if vk_error and not delivered_to_vk:
            data['vk_error'] = vk_error
        return Response(data, status=status.HTTP_201_CREATED)


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


# ── Маппинг типов/источников транзакций бэка → enum мобильного приложения ──────
# Бэк: type=income|expense, source=game|quest|shop|birthday|delivery|manual.
# Мобайл (GuestCoinTxn): type=EARN|SPEND|BIRTHDAY_GIFT|REFERRAL|ADJUST,
#                        source=QR_SCAN|PURCHASE|GAME|QUEST|BIRTHDAY|ADMIN|STORY|REFERRAL.
_MOBILE_TXN_SOURCE = {
    'game':     'GAME',
    'quest':    'QUEST',
    'shop':     'PURCHASE',
    'birthday': 'BIRTHDAY',
    'delivery': 'PURCHASE',
    'manual':   'ADMIN',
}


def _mobile_txn_type(tx) -> str:
    if tx.source == 'manual':
        return 'ADJUST'
    if tx.source == 'birthday' and tx.type == 'income':
        return 'BIRTHDAY_GIFT'
    return 'EARN' if tx.type == 'income' else 'SPEND'


class GuestListAPIView(APIView):
    """
    GET /api/v1/guests/?search=&limit=100&offset=0

    Все гости тенанта с RF-метриками, монетами и датой последнего визита.
    """
    permission_classes = [IsAuthenticated]

    _MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

    def get(self, request):
        from datetime import date, timedelta, timezone as dt_tz
        from django.db.models import Q, Sum, Count, Max
        from apps.shared.guest.models import Client
        from apps.tenant.branch.models import ClientBranch, CoinTransaction, ClientBranchVisit
        from apps.tenant.analytics.models import GuestRFScore

        search = request.query_params.get('search', '').strip()
        try:
            limit = min(int(request.query_params.get('limit', 10000)), 10000)
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 10000, 0

        # Все гости тенанта (с ClientBranch), сортировка по алфавиту — как в Django-админе
        all_client_ids = list(
            ClientBranch.objects.values_list('client_id', flat=True).distinct()
        )
        qs = Client.objects.filter(pk__in=all_client_ids)
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search) |
                Q(vk_id__icontains=search)
            )
        qs = qs.order_by('-created_at', '-pk')
        total = qs.count()
        clients = list(qs[offset:offset + limit])

        if not clients:
            return Response({'guests': [], 'total': total})

        client_ids = [c.pk for c in clients]

        # RF-сегменты (только для emoji/name — могут быть не у всех)
        scores = {
            s.client_id: s
            for s in GuestRFScore.objects.filter(client_id__in=client_ids)
        }

        # ClientBranch PKs → маппинг cb_pk → client_pk
        cb_map: dict[int, list[int]] = {}
        cb_to_client: dict[int, int] = {}
        for cb in ClientBranch.objects.filter(client_id__in=client_ids).values('client_id', 'pk'):
            cb_map.setdefault(cb['client_id'], []).append(cb['pk'])
            cb_to_client[cb['pk']] = cb['client_id']

        all_cb_ids = [pk for pks in cb_map.values() for pk in pks]

        # Монеты
        cb_balance: dict[int, int] = {}
        for row in CoinTransaction.objects.filter(client_id__in=all_cb_ids).values('client_id').annotate(
            income=Sum('amount', filter=Q(type='income')),
            expense=Sum('amount', filter=Q(type='expense')),
        ):
            cb_balance[row['client_id']] = (row['income'] or 0) - (row['expense'] or 0)
        client_coins: dict[int, int] = {
            cid: sum(cb_balance.get(pk, 0) for pk in pks)
            for cid, pks in cb_map.items()
        }

        # Реальные визиты из ClientBranchVisit (не из GuestRFScore — он кешируется)
        client_visit_count: dict[int, int] = {}
        client_last_visit_dt: dict[int, object] = {}
        total_visits = 0
        for row in ClientBranchVisit.objects.filter(client_id__in=all_cb_ids).values('client_id').annotate(
            vcnt=Count('pk'), last_v=Max('visited_at')
        ):
            cid = cb_to_client.get(row['client_id'])
            if cid:
                cnt = row['vcnt']
                client_visit_count[cid] = client_visit_count.get(cid, 0) + cnt
                total_visits += cnt
                existing = client_last_visit_dt.get(cid)
                if existing is None or row['last_v'] > existing:
                    client_last_visit_dt[cid] = row['last_v']

        today = date.today()
        guests = []
        for c in clients:
            frequency = client_visit_count.get(c.pk, 0)
            last_v = client_last_visit_dt.get(c.pk)
            if last_v:
                last_v_date = last_v.date() if hasattr(last_v, 'date') else last_v
                recency_days = (today - last_v_date).days
                last_visit = f"{last_v_date.day} {self._MONTHS[last_v_date.month - 1]}"
            else:
                recency_days = 0
                last_visit = '—'
            guests.append({
                'vk_id':        str(c.vk_id),
                'first_name':   c.first_name or '',
                'last_name':    c.last_name or '',
                'last_visit':   last_visit,
                'frequency':    frequency,
                'recency_days': recency_days,
                'coins':        client_coins.get(c.pk, 0),
            })

        return Response({'guests': guests, 'total': total, 'total_visits': total_visits})


class GuestDetailAPIView(APIView):
    """
    GET /api/v1/guests/<vk_id>/

    Карточка гостя для мобильного приложения. Агрегирует данные по всем
    ClientBranch-профилям гостя в текущем тенанте: баланс монет (income−expense),
    RF-сегмент, последние визиты и транзакции, VK-статус, призы и отзывы.

    Один гость (guest.Client по vk_id) = одна карточка, даже если он состоит
    в нескольких точках сети.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, vk_id: int):
        from django.db.models import Sum, Q
        from apps.shared.guest.models import Client
        from apps.tenant.branch.models import (
            ClientBranch, ClientBranchVisit, CoinTransaction,
        )
        from apps.tenant.inventory.models import InventoryItem

        client = Client.objects.filter(vk_id=vk_id).first()
        if not client:
            return Response({'detail': 'Гость не найден'}, status=status.HTTP_404_NOT_FOUND)

        cbs = list(
            ClientBranch.objects.select_related('branch').filter(client=client)
        )
        if not cbs:
            return Response({'detail': 'Гость не найден в этой сети'}, status=status.HTTP_404_NOT_FOUND)

        cb_ids = [cb.pk for cb in cbs]
        cb_by_id = {cb.pk: cb for cb in cbs}

        # Баланс монет: суммы по всем CB-профилям гостя.
        coin_agg = CoinTransaction.objects.filter(client_id__in=cb_ids).aggregate(
            income=Sum('amount', filter=Q(type='income')),
            expense=Sum('amount', filter=Q(type='expense')),
        )
        total_earned = coin_agg['income'] or 0
        total_spent = coin_agg['expense'] or 0
        coins_balance = total_earned - total_spent

        # RF-метрика (per-Client). Прямой запрос, а не client.rf_score —
        # обратный OneToOne-аксессор кидает RelatedObjectDoesNotExist, если
        # score ещё не рассчитан (getattr с дефолтом его НЕ перехватывает).
        from apps.tenant.analytics.models import GuestRFScore
        recency_days, frequency = 0, 0
        segment_key = segment_emoji = segment_name = None
        score = GuestRFScore.objects.select_related('segment').filter(client=client).first()
        if score:
            recency_days = score.recency_days
            frequency = score.frequency
            segment_key = f'{score.r_score}_{score.f_score}'
            if score.segment_id:
                segment_emoji = score.segment.emoji
                segment_name = score.segment.name

        # Дата регистрации в программе — самый ранний профиль.
        registered_at = min(cb.created_at for cb in cbs)

        # ДР — первый профиль, где он указан.
        birthday = None
        for cb in cbs:
            if cb.birth_date:
                birthday = cb.birth_date.isoformat()
                break

        # VK-статус: OR по всем профилям. Прямой запрос (тот же нюанс
        # обратного OneToOne, что и с rf_score выше).
        from apps.tenant.branch.models import ClientVKStatus
        is_community = is_newsletter = False
        for vks in ClientVKStatus.objects.filter(client_id__in=cb_ids):
            is_community = is_community or vks.is_community_member
            is_newsletter = is_newsletter or vks.is_newsletter_subscriber

        # Последние визиты (до 20).
        visits = list(
            ClientBranchVisit.objects.filter(client_id__in=cb_ids).order_by('-visited_at')[:20]
        )
        recent_visits = []
        for v in visits:
            cb = cb_by_id.get(v.client_id)
            recent_visits.append({
                'id':           v.pk,
                'branch_id':    cb.branch_id if cb else None,
                'branch_name':  cb.branch.name if (cb and cb.branch_id) else '',
                'visited_at':   v.visited_at.isoformat(),
                'table_number': None,
            })
        last_seen_at = visits[0].visited_at.isoformat() if visits else None

        # Транзакции с бегущим балансом: считаем по всей истории, отдаём последние 20.
        all_txns = list(
            CoinTransaction.objects.filter(client_id__in=cb_ids).order_by('created_at', 'id')
        )
        running = 0
        enriched = []
        for tx in all_txns:
            signed = tx.amount if tx.type == 'income' else -tx.amount
            running += signed
            enriched.append((tx, signed, running))
        recent_txns = [{
            'id':            tx.pk,
            'type':          _mobile_txn_type(tx),
            'source':        _MOBILE_TXN_SOURCE.get(tx.source, tx.source.upper()),
            'amount':        signed,
            'balance_after': bal,
            'description':   tx.description or '',
            'created_at':    tx.created_at.isoformat(),
        } for (tx, signed, bal) in reversed(enriched[-20:])]

        # Призы и отзывы.
        prizes_received = InventoryItem.objects.filter(client_branch_id__in=cb_ids).count()
        prizes_activated = InventoryItem.objects.filter(
            client_branch_id__in=cb_ids, activated_at__isnull=False,
        ).count()
        reviews_count = TestimonialConversation.objects.filter(
            Q(client_id__in=cb_ids) | Q(vk_guest_id=client.pk)
        ).distinct().count()

        # Телефон — из последнего отзыва, где гость его указал.
        phone = TestimonialMessage.objects.filter(
            conversation__client_id__in=cb_ids,
        ).exclude(phone='').order_by('-created_at').values_list('phone', flat=True).first() or ''

        return Response({
            'vk_id':         str(client.vk_id),
            'first_name':    client.first_name or '',
            'last_name':     client.last_name or '',
            'phone':         phone,
            'birthday':      birthday,
            'registered_at': registered_at.isoformat(),
            'recency_days':  recency_days,
            'frequency':     frequency,
            'coins_balance': coins_balance,
            'total_earned':  total_earned,
            'total_spent':   total_spent,
            'segment_key':   segment_key,
            'segment_emoji': segment_emoji,
            'segment_name':  segment_name,
            'vk_status': {
                'is_subscribed_community':  is_community,
                'is_subscribed_newsletter': is_newsletter,
                'is_blocked':               not client.is_active,
                'last_seen_at':             last_seen_at,
            },
            'recent_visits':    recent_visits,
            'recent_txns':      recent_txns,
            'prizes_received':  prizes_received,
            'prizes_activated': prizes_activated,
            'reviews_count':    reviews_count,
        })


class AdjustGuestCoinsAPIView(APIView):
    """
    POST /api/v1/guests/<vk_id>/adjust-coins/  body: {amount: int (знаковое), reason: str}

    Ручная корректировка баланса монет гостя администратором.
    amount > 0 — начисление (income), amount < 0 — списание (expense).
    Транзакция привязывается к самому свежему ClientBranch-профилю гостя;
    достаточность баланса проверяется по агрегату всех профилей (как его
    видит мобайл). Требует право adjust_coins.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, vk_id: int):
        from django.db.models import Sum, Q
        from apps.shared.guest.models import Client
        from apps.tenant.branch.models import (
            ClientBranch, CoinTransaction, TransactionType, TransactionSource, AuditLog,
        )
        from apps.tenant.branch.audit import log_audit

        if not _user_has_perm(request.user, 'adjust_coins'):
            return Response(
                {'detail': 'Недостаточно прав для корректировки баланса.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            amount = int(request.data.get('amount'))
        except (TypeError, ValueError):
            return Response({'detail': 'amount должен быть целым числом'}, status=status.HTTP_400_BAD_REQUEST)
        if amount == 0:
            return Response({'detail': 'amount не может быть нулём'}, status=status.HTTP_400_BAD_REQUEST)
        reason = (request.data.get('reason') or '').strip()
        if len(reason) < 3:
            return Response(
                {'detail': 'Укажите причину корректировки (минимум 3 символа).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = Client.objects.filter(vk_id=vk_id).first()
        if not client:
            return Response({'detail': 'Гость не найден'}, status=status.HTTP_404_NOT_FOUND)
        cbs = list(
            ClientBranch.objects.select_related('branch')
            .filter(client=client).order_by('-created_at')
        )
        if not cbs:
            return Response({'detail': 'Гость не найден в этой сети'}, status=status.HTTP_404_NOT_FOUND)
        cb_ids = [cb.pk for cb in cbs]

        with transaction.atomic():
            agg = CoinTransaction.objects.filter(client_id__in=cb_ids).aggregate(
                income=Sum('amount', filter=Q(type='income')),
                expense=Sum('amount', filter=Q(type='expense')),
            )
            balance = (agg['income'] or 0) - (agg['expense'] or 0)
            if amount < 0 and balance + amount < 0:
                return Response(
                    {'detail': 'Недостаточно монет на балансе для списания.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            tx = CoinTransaction.objects.create(
                client=cbs[0],  # самый свежий профиль гостя
                type=TransactionType.INCOME if amount > 0 else TransactionType.EXPENSE,
                source=TransactionSource.MANUAL,
                amount=abs(amount),
                description=reason,
            )
            new_balance = balance + amount

        log_audit(
            request.user, AuditLog.Action.COIN_ADJUST,
            target_type='guest', target_id=client.vk_id,
            target_label=str(client)[:255],
            details=reason[:500],
            delta={'amount': amount, 'balance_after': new_balance},
        )

        return Response({
            'id':            tx.pk,
            'type':          _mobile_txn_type(tx),
            'source':        'ADMIN',
            'amount':        amount,
            'balance_after': new_balance,
            'description':   tx.description or '',
            'created_at':    tx.created_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


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

        # Самовосстановление: если коды на сегодня не сгенерированы (например,
        # celery-beat пропустил тик 03:00 из-за read-only Redis) — создаём их
        # прямо сейчас, при открытии экрана. Идемпотентно (get_or_create).
        try:
            from apps.tenant.branch.tasks import ensure_today_daily_codes
            ensure_today_daily_codes()
        except Exception:
            pass

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
        from apps.tenant.branch.models import Branch, DailyCode, current_code_date

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

        today = current_code_date()
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
        '- По умолчанию коротко (3-4 предложения), без воды. Если в треде менеджер '
        'явно просит написать подробный ответ — выполни, до 4000 символов.\n'
        '- Не используй markdown, HTML, эмодзи кроме одного по необходимости.\n'
        '- Обращайся на "Вы".\n'
        '- Если отзыв негативный — извинись, не оправдывайся, предложи решение.\n'
        '- Если позитивный — поблагодари искренне, без шаблонов.\n'
        '- Не упоминай скидки/компенсации, если не сказано.\n'
        '- Верни ТОЛЬКО текст ответа, без пояснений и подписи.'
    )

    # Подмешиваем инструкции из базы знаний тенанта (тон, факты о заведении,
    # типовые формулировки). Без этого Claude отвечает в отрыве от контекста.
    from apps.tenant.analytics.ai_service import _get_knowledge_base_text
    kb_text = _get_knowledge_base_text()
    if kb_text:
        system_prompt += (
            '\n\n--- Справка о заведении из базы знаний ---\n'
            '(используй для тона общения и фактов; НЕ копируй отсюда '
            'готовые ответы как шаблон)\n'
            + kb_text
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
            max_tokens=2048,
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
        ).prefetch_related('branches').distinct()[: self.LIMIT]
        quests = []
        for qst in qsts:
            branch_names = ', '.join(b.name for b in qst.branches.all()) or '—'
            quests.append({
                'type':     'quest',
                'id':       qst.pk,
                'title':    qst.name,
                'subtitle': f'+{qst.reward} ★ · {branch_names}',
                'match':    '',
                'raw':      {'id': qst.pk, 'name': qst.name, 'reward': qst.reward},
            })

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


_BANK_LABELS = {
    'sberbank': 'Сбербанк',
    'tinkoff':  'Т-Банк',
    'alfabank': 'Альфа-Банк',
    'sbp':      'СБП',
}


class BillingPayAPIView(APIView):
    """
    POST /api/v1/billing/pay/  body: {plan, bank}

    Заглушка оплаты: онлайн-эквайринг пока не подключён. Endpoint фиксирует
    заявку на оплату и отправляет её менеджеру LoyalUP через support-чат
    (с релеем в CheckUp) — менеджер выставит счёт и обновит paid_until.
    Возвращает {payment_url: '', status: 'manager_request', message}, чтобы
    экран оплаты не падал. Реальный платёжный провайдер добавим позже.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.db import connection
        from apps.shared.clients.models import Company
        from apps.tenant.branch.models import SupportChatMessage

        plan = (request.data.get('plan') or '').strip()
        bank = (request.data.get('bank') or '').strip()

        tenant = getattr(connection, 'tenant', None)
        price_rub = getattr(tenant, 'plan_price_rub', None) or 4900
        plan_code = plan or (getattr(tenant, 'plan_code', None) or 'standard')
        try:
            plan_label = Company.Plan(plan_code).label
        except ValueError:
            plan_label = plan_code or 'Стандарт'

        bank_label = _BANK_LABELS.get(bank, bank or '—')
        text = (
            '💳 Заявка на оплату подписки\n'
            f'Тариф: «{plan_label}» — {price_rub} ₽\n'
            f'Способ оплаты: {bank_label}\n'
            f'Запросил: {request.user.get_full_name() or request.user.username}'
        )

        m = SupportChatMessage.objects.create(
            sender=SupportChatMessage.Sender.USER,
            author_id=request.user.pk if request.user.is_authenticated else None,
            text=text,
        )
        _safe_relay_to_checkup(message=m, user=request.user)

        return Response({
            'payment_url': '',
            'status':      'manager_request',
            'message':     'Заявка на оплату принята. Менеджер LoyalUP свяжется '
                           'с вами в чате для выставления счёта.',
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


def _user_has_perm(user, perm_key: str) -> bool:
    """Есть ли у пользователя право perm_key (с учётом роли и StaffProfile)."""
    if user.is_superuser:
        return True
    profile = _get_or_create_staff_profile(user)
    mob_role = _BACKEND_ROLE_TO_MOBILE.get(user.role, 'viewer')
    perms = _merge_permissions(profile.permissions, mob_role)
    return bool(perms.get(perm_key))


# ── RBAC: кто кем может управлять (роли по убыванию: owner > manager > viewer) ──
_ROLE_RANK = {'owner': 3, 'manager': 2, 'viewer': 1}


def _actor_mobile_role(user) -> str:
    if getattr(user, 'is_superuser', False):
        return 'owner'
    return _BACKEND_ROLE_TO_MOBILE.get(getattr(user, 'role', None), 'viewer')


def _can_manage_role(actor, target_mobile_role: str) -> bool:
    """
    Актор может управлять пользователем только СТРОГО ниже своей роли:
      owner → manager + viewer, manager → только viewer, viewer → никого.
    owner-над-owner запрещён (нельзя создать/повысить до owner=superadmin из мобилки).
    """
    actor_rank = _ROLE_RANK.get(_actor_mobile_role(actor), 0)
    target_rank = _ROLE_RANK.get(target_mobile_role, 99)
    return actor_rank > target_rank


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
        # СТРОГО по текущей сети: суперадмины-владельцы (доступ ко всем сетям)
        # + сотрудники именно ЭТОЙ сети. Сотрудников чужих сетей не показываем —
        # иначе можно случайно выдать права человеку не из той сети.
        if tenant is not None and getattr(tenant, 'pk', None):
            qs = qs.filter(Q(role='superadmin') | Q(companies=tenant)).distinct()
        qs = qs.order_by('pk')
        # RBAC: какие роли актор может назначать/создавать (строго ниже своей).
        manageable = [r for r in ('manager', 'viewer') if _can_manage_role(request.user, r)]
        return Response({
            'staff': [_serialize_staff(u) for u in qs],
            'actor_role': _actor_mobile_role(request.user),
            'manageable_roles': manageable,
        })


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
        from django.db import connection
        from apps.tenant.branch.models import Branch
        User = get_user_model()
        try:
            user = User.objects.get(pk=staff_id)
        except User.DoesNotExist:
            return Response({'error': 'Сотрудник не найден'}, status=status.HTTP_404_NOT_FOUND)

        # СТРОГО по сети: редактировать можно только сотрудника ТЕКУЩЕЙ сети
        # (или суперадмина). Чужую сеть не трогаем — переключись на её сеть.
        tenant = getattr(connection, 'tenant', None)
        if (tenant is not None and getattr(tenant, 'pk', None)
                and not user.is_superuser
                and not user.companies.filter(pk=tenant.pk).exists()):
            return Response(
                {'error': 'Этот сотрудник не из текущей сети. Переключитесь на его сеть, чтобы изменить доступы.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile = _get_or_create_staff_profile(user)

        # RBAC: управлять можно только сотрудником СТРОГО ниже своей роли.
        target_current_role = _BACKEND_ROLE_TO_MOBILE.get(user.role, 'viewer')
        if not _can_manage_role(request.user, target_current_role):
            return Response(
                {'error': 'Недостаточно прав для управления этим сотрудником.'},
                status=status.HTTP_403_FORBIDDEN,
            )

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
            # RBAC: нельзя назначить роль не ниже своей (нет эскалации привилегий).
            if not _can_manage_role(request.user, mob_role):
                return Response(
                    {'error': 'Недостаточно прав назначить эту роль.'},
                    status=status.HTTP_403_FORBIDDEN,
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

    def delete(self, request, staff_id: int):
        """Удалить сотрудника из ТЕКУЩЕЙ сети (отвязать от тенанта).
        Если сетей больше не осталось — деактивируем. Юзера физически не
        удаляем (целостность аудита/истории)."""
        from django.contrib.auth import get_user_model
        from django.db import connection
        User = get_user_model()
        try:
            user = User.objects.get(pk=staff_id)
        except User.DoesNotExist:
            return Response({'error': 'Сотрудник не найден'}, status=status.HTTP_404_NOT_FOUND)

        if user.pk == request.user.pk:
            return Response({'error': 'Нельзя удалить самого себя.'}, status=status.HTTP_400_BAD_REQUEST)
        if user.is_superuser:
            return Response({'error': 'Владельца удалить нельзя.'}, status=status.HTTP_403_FORBIDDEN)

        tenant = getattr(connection, 'tenant', None)
        if (tenant is not None and getattr(tenant, 'pk', None)
                and not user.companies.filter(pk=tenant.pk).exists()):
            return Response(
                {'error': 'Этот сотрудник не из текущей сети. Переключитесь на его сеть.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        target_current_role = _BACKEND_ROLE_TO_MOBILE.get(user.role, 'viewer')
        if not _can_manage_role(request.user, target_current_role):
            return Response(
                {'error': 'Недостаточно прав для удаления этого сотрудника.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        label = (user.get_full_name() or user.username)[:255]
        if tenant is not None and getattr(tenant, 'pk', None):
            user.companies.remove(tenant)
        if not user.companies.exists():
            user.is_active = False
            user.save(update_fields=['is_active'])

        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'STAFF_DELETE',
            target_type='staff',
            target_id=user.pk,
            target_label=label,
            details=f'removed_from_tenant={getattr(tenant, "schema_name", None)}',
        )
        return Response({'ok': True})


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
        # RBAC: создавать можно только сотрудника СТРОГО ниже своей роли.
        if not _can_manage_role(request.user, mob_role):
            return Response(
                {'error': 'Недостаточно прав для создания сотрудника с этой ролью.'},
                status=status.HTTP_403_FORBIDDEN,
            )
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
        # Одноразовая выдача учётки — мобайл показывает админу (письма не шлём).
        body['login'] = username
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
    branch_ids = list(q.branches.values_list('pk', flat=True))
    return {
        'id':            q.pk,
        # back-compat: один первый branch_id (для старых клиентов rf-mobile);
        # новое поле — branch_ids (массив всех привязанных точек).
        'branch_id':     branch_ids[0] if branch_ids else q.branch_id,
        'branch_ids':    branch_ids,
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
        from apps.tenant.quest.models import Quest, QuestBranch
        from apps.tenant.branch.models import Branch
        d = request.data

        # Принимаем (в порядке приоритета):
        #   - branch_ids: [1,2,3]   — список точек
        #   - all_branches: true    — все активные точки тенанта
        #   - branch_id: 1          — legacy (одна точка)
        raw_ids = d.get('branch_ids')
        all_branches = bool(d.get('all_branches'))
        legacy_id = d.get('branch_id')

        branch_ids: list[int] = []
        if isinstance(raw_ids, list) and raw_ids:
            try:
                branch_ids = [int(x) for x in raw_ids]
            except (TypeError, ValueError):
                return Response({'error': 'branch_ids должен быть массивом id'}, status=400)
        elif all_branches:
            branch_ids = list(Branch.objects.filter(is_active=True).values_list('pk', flat=True))
        elif legacy_id is not None:
            try:
                branch_ids = [int(legacy_id)]
            except (TypeError, ValueError):
                return Response({'error': 'branch_id должен быть числом'}, status=400)
        else:
            return Response({'error': 'нужны branch_ids / all_branches / branch_id'}, status=400)

        if not branch_ids:
            return Response({'error': 'нет активных точек'}, status=400)

        existing = set(Branch.objects.filter(pk__in=branch_ids).values_list('pk', flat=True))
        missing = [b for b in branch_ids if b not in existing]
        if missing:
            return Response({'error': f'Точки не найдены: {missing}'}, status=404)

        name = (d.get('name') or '').strip()
        if not name:
            return Response({'error': 'name обязателен'}, status=400)
        try:
            reward = int(d.get('reward') or 0)
        except (TypeError, ValueError):
            return Response({'error': 'reward должен быть числом'}, status=400)

        q = Quest.objects.create(
            name=name[:255],
            description=(d.get('description') or '')[:2000],
            reward=max(0, reward),
            is_active=bool(d.get('is_active', True)),
            ordering=int(d.get('ordering') or 0),
        )
        for bid in branch_ids:
            QuestBranch.objects.create(quest=q, branch_id=bid, ordering=q.ordering, is_active=q.is_active)
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

        # Полностью переставляем привязки к точкам, если клиент прислал branch_ids
        # или all_branches=true. Игнорируем legacy одиночный branch_id в PATCH
        # (создание новой связи делается через POST).
        from apps.tenant.quest.models import QuestBranch
        from apps.tenant.branch.models import Branch
        new_ids: list[int] | None = None
        if isinstance(d.get('branch_ids'), list):
            try:
                new_ids = [int(x) for x in d['branch_ids']]
            except (TypeError, ValueError):
                return Response({'error': 'branch_ids должен быть массивом id'}, status=400)
        elif d.get('all_branches') is True:
            new_ids = list(Branch.objects.filter(is_active=True).values_list('pk', flat=True))
        if new_ids is not None:
            existing = set(Branch.objects.filter(pk__in=new_ids).values_list('pk', flat=True))
            missing = [b for b in new_ids if b not in existing]
            if missing:
                return Response({'error': f'Точки не найдены: {missing}'}, status=404)
            QuestBranch.objects.filter(quest=q).delete()
            for bid in new_ids:
                QuestBranch.objects.create(quest=q, branch_id=bid, ordering=q.ordering, is_active=q.is_active)
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
    """
    GET /api/v1/support/chat/manager/

    Returns manager metadata PLUS chat state (recent messages, unread count,
    last message preview). Mobile polls this every few seconds — having
    everything in one response means new manager replies appear without
    a separate /messages/ fetch.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        from apps.tenant.branch.models import SupportChatMessage

        info = dict(_SUPPORT_MANAGER)
        info['last_seen'] = timezone.now().isoformat()

        # Chat history — last 200 messages, oldest-first (UI render-friendly).
        msgs = list(SupportChatMessage.objects.order_by('-created_at')[:200])
        msgs.reverse()

        # Mark unread manager messages as read (mirrors /messages/ behavior).
        now = timezone.now()
        unread = [m for m in msgs if m.sender == SupportChatMessage.Sender.MANAGER and not m.read_at]
        if unread:
            SupportChatMessage.objects.filter(
                pk__in=[m.pk for m in unread]
            ).update(read_at=now)
            for m in unread:
                m.read_at = now

        # Chat state preview (for badge / list rendering).
        last = msgs[-1] if msgs else None
        info['messages'] = [_serialize_support_message(m) for m in msgs]
        info['unread_count'] = 0  # manager messages just marked read above
        info['last_message_at'] = last.created_at.isoformat() if last else None
        info['last_message_text'] = (last.text[:200] if last else '')
        info['last_message_sender'] = (last.sender if last else None)

        return Response(info)


def _safe_relay_to_checkup(*, message, user) -> None:
    """
    Best-effort POST to CheckUp side (POST /api/v1/loyalup/inbound/).
    Never raises — logs on failure. CheckUp handles its own retries on their side
    (LoyalupRelayOutbox + Celery), so for now we just fire-and-forget.

    Called from SupportChatMessagesAPIView.post() for user-side messages only.
    Manager-side messages (sender_role=manager) are echo of CheckUp content and
    must not loop back.
    """
    import logging
    import requests
    from django.conf import settings
    from django.db import connection

    log = logging.getLogger(__name__)
    secret = getattr(settings, "LOYALUP_RELAY_SECRET", "") or ""
    url = getattr(settings, "CHECKUP_RELAY_URL", "") or "http://localhost:8000/api/v1/loyalup/inbound/"
    if not secret:
        log.warning("_safe_relay_to_checkup: LOYALUP_RELAY_SECRET not set — skip msg=%s", message.pk)
        return

    tenant = getattr(connection, "tenant", None)
    if not tenant:
        log.warning("_safe_relay_to_checkup: no tenant on connection — skip msg=%s", message.pk)
        return

    author_id = None
    author_name = ""
    if user and getattr(user, "is_authenticated", False):
        author_id = user.pk
        author_name = (user.get_full_name() or getattr(user, "username", "") or "").strip()
    if not author_name:
        author_name = "Гость"

    payload = {
        "tenant_schema": tenant.schema_name,
        "tenant_name":   getattr(tenant, "name", tenant.schema_name),
        "message_id":    message.pk,
        "text":          message.text,
        "author_id":     author_id,
        "author_name":   author_name,
        "created_at":    message.created_at.isoformat() if message.created_at else None,
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"X-LoyalUP-Relay-Secret": secret, "Content-Type": "application/json"},
            timeout=5,
        )
        if r.status_code != 201:
            log.warning(
                "_safe_relay_to_checkup: unexpected status=%s body=%s msg=%s tenant=%s",
                r.status_code, r.text[:300], message.pk, tenant.schema_name,
            )
    except requests.RequestException as e:
        log.warning(
            "_safe_relay_to_checkup: request failed msg=%s tenant=%s err=%s",
            message.pk, tenant.schema_name, e,
        )


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

        # Outbound relay to CheckUp (only user-side; manager echo would loop).
        if sender == SupportChatMessage.Sender.USER:
            _safe_relay_to_checkup(message=m, user=request.user)

        return Response(_serialize_support_message(m), status=201)


# ════════════════════════════════════════════════════════════════════
# AI-ассистент «Лояльчик» — отвечает на вопросы по системе
# ════════════════════════════════════════════════════════════════════
_ASSISTANT_SYSTEM_PROMPT = """Ты — «Лояльчик», дружелюбный AI-ассистент мобильного приложения LoyalUP.
LoyalUP — это приложение для ВЛАДЕЛЬЦЕВ и управляющих кафе/ресторанов: оно управляет
программой лояльности гостей в мини-приложении ВКонтакте.

Твоя задача — помогать владельцу разобраться, КАК пользоваться приложением и системой.
Отвечай коротко, по-доброму, на русском, по делу (2–5 предложений). Без воды.
Лёгкий космический тон уместен, но без перебора (ты «выводишь лояльность на орбиту» 🚀).

Что умеет приложение (по разделам):
- Главная: задачи дня, ключевые показатели, коды дня, дни рождения гостей.
- Аналитика (RF): сегменты гостей по давности (R) и частоте (F) визитов — чемпионы,
  группа риска, потерянные; миграция сегментов; пороги RF можно настроить.
- Отзывы: отзывы из ВК и приложения, AI определяет тональность, можно ответить гостю
  прямо из приложения; есть AI-черновики ответов.
- Чат: связь с поддержкой LoyalUP.
- Ещё: рассылки (VK), коды дня (игра/квест/день рождения), каталог призов, категории,
  квесты, акции, гости, отчёты (PDF, можно выбрать разделы), сотрудники (роли и доступы),
  настройки автоответов, профиль, переключение сети (если их несколько).
- Брендинг (цвета/логотип сети) настраивается в веб-админке, не в приложении.

Правила:
- Отвечай ТОЛЬКО про LoyalUP и работу с приложением/программой лояльности.
- Если вопрос не по теме или ты не уверен — честно скажи и предложи написать в поддержку
  (раздел «Чат»). Не выдумывай функции, которых нет.
- Не обещай того, чего система не делает."""


def _assistant_tenant_context() -> dict:
    """Лёгкая сводка по текущей сети для контекста Лояльчика (только числа)."""
    ctx: dict = {}
    try:
        from django.utils import timezone
        from datetime import timedelta
        from apps.tenant.branch.models import TestimonialConversation
        since = timezone.now() - timedelta(days=30)
        base = TestimonialConversation.objects.filter(last_message_at__gte=since)
        ctx['unanswered_negatives'] = base.filter(
            sentiment='NEGATIVE', is_replied=False,
        ).count()
        ctx['drafts_ready'] = base.filter(is_replied=False).exclude(
            ai_draft='').exclude(ai_draft__isnull=True).count()
    except Exception:
        pass
    try:
        from django.utils import timezone
        from apps.tenant.branch.models import ClientBranch
        today = timezone.localdate()
        ctx['birthdays_today'] = ClientBranch.objects.filter(
            birth_date__month=today.month, birth_date__day=today.day,
        ).count()
    except Exception:
        pass
    return ctx


def _assistant_context_text(ctx: dict) -> str:
    """Человекочитаемая сводка для системного промпта (чтобы AI знал цифры)."""
    parts = []
    if ctx.get('unanswered_negatives'):
        parts.append(f"неотвеченных негативных отзывов: {ctx['unanswered_negatives']}")
    if ctx.get('drafts_ready'):
        parts.append(f"готовых AI-черновиков ответов: {ctx['drafts_ready']}")
    if ctx.get('birthdays_today'):
        parts.append(f"дней рождения гостей сегодня: {ctx['birthdays_today']}")
    if not parts:
        return ('Текущая сводка по сети: срочных дел не видно — неотвеченных '
                'негативов нет.')
    return 'Текущая сводка по сети владельца (используй эти числа в ответах): ' + '; '.join(parts) + '.'


# Действия-кнопки под ответом Лояльчика. `screen` — из закрытого набора, который
# мобилка умеет открыть (см. navBridge.ts). Порядок правил = приоритет.
_ASSISTANT_ACTION_RULES = [
    (('отзыв', 'ответить на отзыв', 'негатив', 'жалоб'), 'reviews', 'Открыть отзывы'),
    (('рассылк', 'broadcast', 'кампани'), 'campaigns', 'Открыть рассылки'),
    (('код дня', 'коды дня', 'ежедневн'), 'daily-codes', 'Коды дня'),
    (('сотрудник', 'персонал', 'роль', 'права доступ', 'пригласить'), 'staff', 'Сотрудники'),
    (('рожден', 'день рождения'), 'guests', 'Гости (ДР)'),
    (('rfm', 'rf-сегмент', 'сегмент', 'аналитик', 'отчёт', 'отчет'), 'analytics', 'Аналитика'),
    (('квест', 'задани'), 'quests', 'Квесты'),
    (('акци', 'промо', 'скидк'), 'promotions', 'Акции'),
    (('меню', 'каталог', 'товар', 'блюд'), 'catalog', 'Каталог'),
    (('порог',), 'rf-thresholds', 'Пороги RF'),
    (('автоответ', 'авто-ответ', 'шаблон ответ'), 'auto-reply', 'Автоответы'),
    (('гост', 'клиент', 'посетител'), 'guests', 'Гости'),
    (('поддержк', 'саппорт', 'техподдержк'), 'chat', 'Чат с поддержкой'),
]


def _assistant_actions(question: str, answer: str) -> list:
    """Подбирает до 2 кнопок-действий по ключевым словам (вопрос важнее ответа)."""
    q = (question or '').lower()
    a = (answer or '').lower()
    actions, seen = [], set()
    for source in (q, a):  # сначала по вопросу (намерение), потом по ответу
        for keywords, screen, label in _ASSISTANT_ACTION_RULES:
            if screen in seen:
                continue
            if any(kw in source for kw in keywords):
                actions.append({'label': label, 'screen': screen})
                seen.add(screen)
                if len(actions) >= 2:
                    return actions
    return actions


class AssistantAskAPIView(APIView):
    """
    POST /api/v1/assistant/ask/
    body: {question: str, history?: [{role: 'user'|'assistant', content: str}]}
    Возвращает {answer}. AI «Лояльчик» — ответы по системе через Claude (прокси).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import logging
        logger = logging.getLogger(__name__)

        question = (request.data.get('question') or '').strip()
        if not question:
            return Response({'error': 'Пустой вопрос'}, status=status.HTTP_400_BAD_REQUEST)
        if len(question) > 1000:
            question = question[:1000]

        # Контекст диалога (последние реплики) — для связного ответа.
        history = request.data.get('history') or []
        msgs = []
        if isinstance(history, list):
            for h in history[-10:]:
                if not isinstance(h, dict):
                    continue
                role = h.get('role')
                content = (h.get('content') or '').strip()
                if role in ('user', 'assistant') and content:
                    msgs.append({'role': role, 'content': content[:2000]})
        msgs.append({'role': 'user', 'content': question})

        try:
            import os
            import anthropic
            from django.conf import settings
            api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
            if not api_key:
                return Response(
                    {'error': 'AI временно недоступен'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            proxy_url = os.getenv('AI_PROXY_URL', '')
            client = (anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
                      if proxy_url else anthropic.Anthropic(api_key=api_key))
            # Контекст по сети — чтобы Лояльчик отвечал реальными цифрами.
            ctx_text = _assistant_context_text(_assistant_tenant_context())
            system_prompt = _ASSISTANT_SYSTEM_PROMPT + ('\n\n' + ctx_text if ctx_text else '')
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=600,
                system=system_prompt,
                messages=msgs,
            )
            answer = (resp.content[0].text or '').strip()
            if not answer:
                answer = 'Хм, не смог сформулировать ответ. Попробуй переформулировать вопрос 🚀'
            return Response({'answer': answer, 'actions': _assistant_actions(question, answer)})
        except Exception as e:
            logger.warning('AssistantAsk failed: %s', e)
            return Response(
                {'error': 'Лояльчик засмотрелся на звёзды и не ответил. Попробуйте ещё раз.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class AssistantContextAPIView(APIView):
    """
    GET /api/v1/assistant/context/
    Проактивное приветствие Лояльчика по реальной сводке + быстрые вопросы.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = _assistant_tenant_context()
        un = ctx.get('unanswered_negatives') or 0
        bdays = ctx.get('birthdays_today') or 0
        if un:
            greeting = (f'Привет! Я Лояльчик 🚀 Вижу {un} неотвеченных негативных '
                        f'отзывов — стоит ответить, пока гости не остыли. Спроси, если нужна помощь!')
        elif bdays:
            greeting = (f'Привет! Я Лояльчик 🚀 Сегодня дни рождения у {bdays} гостей — '
                        f'отличный повод их порадовать. Чем помочь?')
        else:
            greeting = ('Привет! Я Лояльчик 🚀 Помогу разобраться с приложением и '
                        'программой лояльности. Спроси что угодно!')
        suggestions = [
            'Как ответить на отзыв?',
            'Что такое RF-сегменты?',
            'Как запустить рассылку?',
            'Как настроить коды дня?',
        ]
        return Response({'greeting': greeting, 'suggestions': suggestions, 'stats': ctx})
