"""
Analytics API views — request/response only, no business logic.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .serializers import StatsQuerySerializer, RFQuerySerializer
from . import services


class GeneralStatsAPIView(APIView):
    """
    GET /api/v1/analytics/stats/

    Query params:
      branch_ids — comma-separated Branch PKs (omit = all branches)
      period     — today | 7d | 30d | 90d | year | all  (default: 30d)
      start      — YYYY-MM-DD  (overrides period)
      end        — YYYY-MM-DD  (overrides period)
    """

    @extend_schema(parameters=[StatsQuerySerializer], responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        ser = StatsQuerySerializer(data=request.query_params)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        branch_ids = ser.validated_data['branch_ids'] or None
        start_date = ser.validated_data['start']
        end_date   = ser.validated_data['end']

        stats  = services.get_general_stats(branch_ids, start_date, end_date)
        charts = services.get_chart_data(branch_ids, start_date, end_date)

        return Response({
            'stats':  stats,
            'charts': charts,
            'meta': {
                'start':      str(start_date),
                'end':        str(end_date),
                'branch_ids': branch_ids or [],
            },
        })


class RFStatsAPIView(APIView):
    """
    GET /api/v1/analytics/rf/

    Query params:
      branch_ids — comma-separated Branch PKs (omit = all branches)
      mode       — restaurant | delivery (default: restaurant)
      trend_days — number of days for trend chart (7–365, default: 30)
      r_score    — when combined with f_score, returns guest list for that cell
      f_score    — see r_score
    """

    @extend_schema(parameters=[RFQuerySerializer], responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        ser = RFQuerySerializer(data=request.query_params)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        branch_ids = ser.validated_data['branch_ids'] or None
        mode       = ser.validated_data['mode']
        trend_days = ser.validated_data['trend_days']
        r_score    = ser.validated_data.get('r_score')
        f_score    = ser.validated_data.get('f_score')
        start_date = ser.validated_data.get('start')
        end_date   = ser.validated_data.get('end')

        # Guest list for a specific matrix cell.
        # Если есть период — фильтруем список тем же набором guest.Client,
        # что попадает в матрицу (чтобы count и длина списка были согласованы,
        # и 1 гость = 1 строка, даже если он есть в нескольких точках).
        if r_score is not None and f_score is not None:
            active_client_ids = None
            if start_date and end_date:
                active_client_ids = services._get_active_client_ids_v2(
                    branch_ids, start_date, end_date, mode,
                )
            guests = services.get_rf_segment_guests(
                branch_ids, r_score, f_score, mode=mode,
                client_ids=active_client_ids,
            )
            matrix = services.get_rf_matrix(
                branch_ids, mode=mode, client_ids=active_client_ids,
            )
            cell   = matrix['cells'].get(f'{r_score}_{f_score}', {})
            return Response({
                'guests':       guests,
                'segment_name': cell.get('segment_name', '—'),
                'count':        cell.get('count', 0),
            })

        rf = services.get_rf_stats(branch_ids, mode=mode, start_date=start_date, end_date=end_date)
        return Response({
            'matrix':     rf['matrix'],
            'trend':      rf['trend'],
            'migrations': rf['migrations'],
            # Активные пороги и их источник — фронт может использовать для
            # отрисовки заголовков матрицы и подсказок «Все точки vs точка X».
            'thresholds':        rf['matrix'].get('thresholds'),
            'thresholds_source': rf['matrix'].get('thresholds_source'),
        })


class RecalculateRFView(APIView):
    """
    POST /api/v1/analytics/rf/recalculate/

    Synchronously recalculates RF scores for the given branches and mode.
    Intended for manual runs from the admin dashboard.

    Body (JSON or form):
      mode       — restaurant | delivery  (default: restaurant)
      branch_ids — comma-separated Branch PKs (omit = all active branches)
    """

    @extend_schema(request=RFQuerySerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        ser = RFQuerySerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        branch_ids = ser.validated_data['branch_ids'] or None
        mode       = ser.validated_data['mode']

        result = services.recalculate_rf_scores(branch_ids=branch_ids, mode=mode)
        return Response(result, status=status.HTTP_200_OK)


class RFThresholdsAPIView(APIView):
    """
    PATCH /api/v1/analytics/rf/thresholds/

    Body (JSON):
      r_fresh_max, r_warm_max, r_cooling_max — R-границы (дни), строго возрастают
      f_rare_max, f_moderate_max             — F-границы (визиты), строго возрастают
      branch_id (опц.)                       — обновить пороги одной точки;
                                              без него — обновить «Все точки» (branch=NULL).

    Создаёт или обновляет RFSettings в текущей tenant-схеме. post_save-сигнал
    автоматически синхронизирует RFSegment.recency/frequency границы.
    """
    permission_classes = [IsAuthenticated]

    REQUIRED_FIELDS = (
        'r_fresh_max', 'r_warm_max', 'r_cooling_max',
        'f_rare_max', 'f_moderate_max',
    )

    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    def patch(self, request):
        from apps.tenant.analytics.models import RFSettings
        from apps.tenant.branch.models import Branch

        # 1) Парсим и валидируем все 5 порогов.
        values: dict[str, int] = {}
        for key in self.REQUIRED_FIELDS:
            raw = request.data.get(key)
            if raw is None:
                return Response(
                    {'error': f'Поле «{key}» обязательно'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                v = int(raw)
            except (TypeError, ValueError):
                return Response(
                    {'error': f'Поле «{key}» должно быть целым числом'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if v < 1:
                return Response(
                    {'error': f'Поле «{key}» должно быть ≥ 1'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            values[key] = v

        # 2) Проверка возрастания границ ДО записи (то же, что RFSettings.clean()).
        if not (values['r_fresh_max'] < values['r_warm_max'] < values['r_cooling_max']):
            return Response(
                {'error': 'R-границы должны идти строго возрастающе: R3 < R2 < R1'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not (values['f_rare_max'] < values['f_moderate_max']):
            return Response(
                {'error': 'F-границы должны идти строго возрастающе: F1 < F2'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 3) Резолвим точку (если передана).
        branch = None
        branch_id = request.data.get('branch_id')
        if branch_id is not None and branch_id != '':
            try:
                branch = Branch.objects.get(pk=int(branch_id))
            except (Branch.DoesNotExist, TypeError, ValueError):
                return Response(
                    {'error': 'Точка не найдена'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # 4) Сохраняем (post_save-сигнал синхронизирует RFSegment-границы).
        obj, _created = RFSettings.objects.update_or_create(
            branch=branch,
            defaults=values,
        )

        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'THRESHOLDS_SAVE',
            target_type='thresholds',
            target_id=obj.pk,
            target_label=obj.scope_label,
            details=f'R3≤{values["r_fresh_max"]}д, R2≤{values["r_warm_max"]}д, R1≤{values["r_cooling_max"]}д · F1≤{values["f_rare_max"]}, F2≤{values["f_moderate_max"]}',
            delta={'after': values},
        )

        return Response({
            'ok': True,
            'thresholds': obj.thresholds_dict(),
            'scope': obj.scope_label,
            'is_global': obj.is_global,
        })


class AutoReplySettingsAPIView(APIView):
    """
    GET   /api/v1/analytics/auto-reply/settings/
    PATCH /api/v1/analytics/auto-reply/settings/

    Singleton-настройки AI-автоответов на отзывы (одна запись на тенант).

    Body (PATCH, все поля опциональны):
      enabled            — bool, общий рубильник
      sentiment_enabled  — dict{POSITIVE,NEGATIVE,PARTIALLY_NEGATIVE,NEUTRAL,PENDING: bool}
                          (SPAM всегда выключен — не принимаем в API)
      branch_enabled     — dict{branch_id(str): bool}
      reminder_minutes   — 30 | 60 | 180 | 720
      ai_tone            — 'formal' | 'friendly' | 'neutral'
    """
    permission_classes = [IsAuthenticated]

    SENTIMENT_MAP = {
        'POSITIVE':           'sentiment_positive',
        'NEGATIVE':           'sentiment_negative',
        'PARTIALLY_NEGATIVE': 'sentiment_partially_negative',
        'NEUTRAL':            'sentiment_neutral',
        'PENDING':            'sentiment_pending',
    }

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        from apps.tenant.branch.models import ReviewAutoReplyConfig
        cfg = ReviewAutoReplyConfig.get_singleton()
        return Response(cfg.to_mobile_dict())

    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def patch(self, request):
        from apps.tenant.branch.models import ReviewAutoReplyConfig
        cfg = ReviewAutoReplyConfig.get_singleton()
        d = request.data or {}

        if 'enabled' in d:
            cfg.enabled = bool(d['enabled'])

        sent = d.get('sentiment_enabled')
        if sent is not None:
            if not isinstance(sent, dict):
                return Response(
                    {'error': 'sentiment_enabled должен быть объектом'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for key, attr in self.SENTIMENT_MAP.items():
                if key in sent:
                    setattr(cfg, attr, bool(sent[key]))

        if 'branch_enabled' in d:
            be = d.get('branch_enabled') or {}
            if not isinstance(be, dict):
                return Response(
                    {'error': 'branch_enabled должен быть объектом'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                normalized = {str(int(k)): bool(v) for k, v in be.items()}
            except (TypeError, ValueError):
                return Response(
                    {'error': 'branch_enabled: ключи должны быть числовыми branch_id'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cfg.branch_enabled = normalized

        if 'reminder_minutes' in d:
            try:
                rm = int(d['reminder_minutes'])
            except (TypeError, ValueError):
                return Response(
                    {'error': 'reminder_minutes должен быть числом'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            valid = [c.value for c in ReviewAutoReplyConfig.Reminder]
            if rm not in valid:
                return Response(
                    {'error': f'reminder_minutes: допустимы {valid}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cfg.reminder_minutes = rm

        if 'ai_tone' in d:
            tone = d['ai_tone']
            valid_tones = [c.value for c in ReviewAutoReplyConfig.Tone]
            if tone not in valid_tones:
                return Response(
                    {'error': f'ai_tone: допустимы {valid_tones}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cfg.ai_tone = tone

        cfg.save()

        from apps.tenant.branch.audit import log_audit
        log_audit(
            request.user, 'AUTO_REPLY_SAVE',
            target_type='thresholds',
            target_id=cfg.pk,
            target_label='Авто-ответы AI',
            delta={'after': cfg.to_mobile_dict()},
        )
        return Response(cfg.to_mobile_dict())


class EngagementAnalyticsAPIView(APIView):
    """
    GET /api/v1/analytics/engagement/?period_days=30&branch_id=7

    Возвращает аналитику по подаркам (InventoryItem) и квестам (QuestSubmit)
    за период: суммарные показатели + per-gift и per-quest разбивку с трендом
    относительно предыдущего периода.

    Параметры:
      period_days  — 1..365 (по умолчанию 30)
      branch_id    — опц., фильтр по торговой точке
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        from datetime import timedelta
        from django.db.models import Count, Q, F, Avg, ExpressionWrapper, DurationField
        from django.utils import timezone
        from apps.tenant.inventory.models import InventoryItem
        from apps.tenant.quest.models import QuestSubmit

        try:
            period_days = int(request.query_params.get('period_days', 30))
        except (TypeError, ValueError):
            period_days = 30
        period_days = max(1, min(period_days, 365))

        branch_id_raw = request.query_params.get('branch_id') or ''
        try:
            branch_id = int(branch_id_raw) if branch_id_raw else None
        except ValueError:
            branch_id = None

        now = timezone.now()
        since = now - timedelta(days=period_days)
        prev_since = since - timedelta(days=period_days)

        # ── Gifts (InventoryItem) ───────────────────────────────────────────
        gift_qs = InventoryItem.objects.filter(created_at__gte=since)
        prev_gift_qs = InventoryItem.objects.filter(
            created_at__gte=prev_since, created_at__lt=since,
        )
        if branch_id is not None:
            gift_qs = gift_qs.filter(client_branch__branch_id=branch_id)
            prev_gift_qs = prev_gift_qs.filter(client_branch__branch_id=branch_id)

        gift_rows = list(
            gift_qs.values(
                'product_id', 'product__name', 'product__price', 'product__emoji',
            )
            .annotate(
                redeemed_count=Count('id'),
                activated_count=Count('id', filter=Q(activated_at__isnull=False)),
                expired_count=Count(
                    'id',
                    filter=Q(used_at__isnull=True, expires_at__isnull=False, expires_at__lt=now),
                ),
            )
            .order_by('-redeemed_count')
        )
        prev_gift_map = {
            r['product_id']: r['redeemed_count']
            for r in prev_gift_qs.values('product_id').annotate(redeemed_count=Count('id'))
        }

        # Категории через ProductBranch (если указана конкретная точка — её
        # категория; иначе — любая первая).
        from apps.tenant.catalog.models import ProductBranch
        product_ids_in_results = [r['product_id'] for r in gift_rows if r['product_id']]
        cat_qs = ProductBranch.objects.filter(product_id__in=product_ids_in_results).select_related('category')
        if branch_id is not None:
            cat_qs = cat_qs.filter(branch_id=branch_id)
        category_map: dict[int, str] = {}
        for pb in cat_qs:
            if pb.product_id not in category_map and pb.category_id:
                category_map[pb.product_id] = pb.category.name

        gifts = []
        for r in gift_rows:
            if r['product_id'] is None:
                continue
            redeemed = r['redeemed_count'] or 0
            activated = r['activated_count'] or 0
            conversion = round(activated / redeemed * 100, 1) if redeemed else 0.0
            prev_redeemed = prev_gift_map.get(r['product_id'], 0)
            trend = (
                round((redeemed - prev_redeemed) / prev_redeemed * 100, 1)
                if prev_redeemed else 0.0
            )
            gifts.append({
                'product_id':      r['product_id'],
                'product_name':    r['product__name'] or '—',
                'product_emoji':   r['product__emoji'] or '',
                'category_name':   category_map.get(r['product_id'], ''),
                'price_coins':     r['product__price'] or 0,
                'redeemed_count':  redeemed,
                'activated_count': activated,
                'expired_count':   r['expired_count'] or 0,
                'conversion_rate': conversion,
                'trend_pct':       trend,
            })

        # ── Quests (QuestSubmit) ────────────────────────────────────────────
        quest_qs = QuestSubmit.objects.filter(created_at__gte=since)
        prev_quest_qs = QuestSubmit.objects.filter(
            created_at__gte=prev_since, created_at__lt=since,
        )
        if branch_id is not None:
            quest_qs = quest_qs.filter(quest__branch_id=branch_id)
            prev_quest_qs = prev_quest_qs.filter(quest__branch_id=branch_id)

        quest_rows = list(
            quest_qs.values(
                'quest_id', 'quest__name', 'quest__reward', 'quest__is_active',
            )
            .annotate(
                started_count=Count('id'),
                completed_count=Count('id', filter=Q(completed_at__isnull=False)),
                avg_dur=Avg(
                    ExpressionWrapper(
                        F('completed_at') - F('created_at'),
                        output_field=DurationField(),
                    ),
                    filter=Q(completed_at__isnull=False),
                ),
            )
            .order_by('-completed_count')
        )
        prev_quest_map = {
            r['quest_id']: r['completed_count']
            for r in prev_quest_qs.values('quest_id').annotate(
                completed_count=Count('id', filter=Q(completed_at__isnull=False)),
            )
        }

        quests = []
        for r in quest_rows:
            if r['quest_id'] is None:
                continue
            started = r['started_count'] or 0
            completed = r['completed_count'] or 0
            completion = round(completed / started * 100, 1) if started else 0.0
            prev_completed = prev_quest_map.get(r['quest_id'], 0)
            trend = (
                round((completed - prev_completed) / prev_completed * 100, 1)
                if prev_completed else 0.0
            )
            avg_hours = (
                round(r['avg_dur'].total_seconds() / 3600, 1)
                if r.get('avg_dur') else 0.0
            )
            quests.append({
                'quest_id':             r['quest_id'],
                'quest_name':           r['quest__name'] or '—',
                'reward_coins':         r['quest__reward'] or 0,
                'is_active':            bool(r['quest__is_active']),
                'started_count':        started,
                'completed_count':      completed,
                'completion_rate':      completion,
                'avg_completion_hours': avg_hours,
                'trend_pct':            trend,
            })

        # ── Summary ─────────────────────────────────────────────────────────
        g_red = sum(g['redeemed_count'] for g in gifts)
        g_act = sum(g['activated_count'] for g in gifts)
        g_conv = round(g_act / g_red * 100, 1) if g_red else 0.0
        g_coins = sum(g['price_coins'] * g['redeemed_count'] for g in gifts)

        q_started = sum(q['started_count'] for q in quests)
        q_completed = sum(q['completed_count'] for q in quests)
        q_compl_rate = round(q_completed / q_started * 100, 1) if q_started else 0.0
        weighted_hours = sum(q['avg_completion_hours'] * q['completed_count'] for q in quests)
        q_avg_hours = round(weighted_hours / q_completed, 1) if q_completed else 0.0

        period_label = (
            'За сегодня' if period_days == 1
            else f'За {period_days} дней' if period_days < 365
            else 'За год'
        )

        return Response({
            'summary': {
                'period_label':                period_label,
                'gifts_redeemed_total':        g_red,
                'gifts_activated_total':       g_act,
                'gifts_avg_conversion':        g_conv,
                'gifts_total_coins_spent':     g_coins,
                'quests_started_total':        q_started,
                'quests_completed_total':      q_completed,
                'quests_avg_completion':       q_compl_rate,
                'quests_avg_completion_hours': q_avg_hours,
            },
            'gifts':  gifts,
            'quests': quests,
        })


class CampaignsHistoryAPIView(APIView):
    """
    GET /api/v1/analytics/campaigns/?limit=100

    История запусков рассылок (BroadcastSend) в формате,
    ожидаемом мобильным приложением.
    """
    permission_classes = [IsAuthenticated]

    SEND_STATUS_TO_CAMPAIGN = {
        'pending':   'scheduled',
        'running':   'sending',
        'done':      'sent',
        'failed':    'failed',
        'cancelled': 'failed',
    }
    GENDER_TO_FILTER = {'all': 'all', 'm': 'male', 'f': 'female'}

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        from apps.tenant.senler.models import BroadcastSend

        try:
            limit = int(request.query_params.get('limit', 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))

        sends = (
            BroadcastSend.objects
            .select_related('broadcast', 'broadcast__branch', 'auto_broadcast_template')
            .prefetch_related('broadcast__rf_segments')
            .order_by('-created_at')[:limit]
        )

        campaigns = []
        for s in sends:
            br = s.broadcast
            seg = None
            if br is not None:
                seg = br.rf_segments.first()

            # segment_key из кода RFSegment ('R3F3' → '3_3')
            seg_key = ''
            seg_name = ''
            seg_emoji = ''
            if seg:
                code = seg.code or ''
                if len(code) == 4 and code[0] == 'R' and code[2] == 'F':
                    seg_key = f'{code[1]}_{code[3]}'
                else:
                    seg_key = code
                seg_name = seg.name or ''
                seg_emoji = seg.emoji or ''
            elif br is None and s.auto_broadcast_template_id:
                seg_name = str(s.auto_broadcast_template)

            image_uri = ''
            if br and br.image:
                try:
                    image_uri = br.image.url
                except Exception:
                    image_uri = ''

            sent_at = (s.finished_at or s.started_at or s.created_at).isoformat()

            campaigns.append({
                'id':            s.pk,
                'segment_key':   seg_key,
                'segment_name':  seg_name or 'Все оцифрованные',
                'segment_emoji': seg_emoji or '📣',
                'sent_at':       sent_at,
                'total_sent':    s.sent_count or 0,
                'total_target':  s.recipients_count or 0,
                'message_text':  br.message_text if br else '',
                'image_uri':     image_uri or None,
                'status':        self.SEND_STATUS_TO_CAMPAIGN.get(s.status, 'failed'),
                'channel':       'vk',
                'gender_filter': self.GENDER_TO_FILTER.get(
                    br.gender_filter if br else 'all', 'all'
                ),
            })

        return Response({'campaigns': campaigns})


class SlowStatsAPIView(APIView):
    """
    GET /api/v1/analytics/stats/slow/

    Returns only the slow-to-compute stats (POS guests + scan index).
    Called asynchronously from the dashboard after the page has loaded.

    Query params: same as GeneralStatsAPIView (branch_ids, period, start, end)
    """

    @extend_schema(parameters=[StatsQuerySerializer], responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        ser = StatsQuerySerializer(data=request.query_params)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        branch_ids = ser.validated_data['branch_ids'] or None
        start_date = ser.validated_data['start']
        end_date   = ser.validated_data['end']

        pos   = services.get_pos_guests_count(branch_ids, start_date, end_date)
        scans = services.get_qr_scan_count(branch_ids, start_date, end_date)
        return Response({
            'pos_guests': pos,
            'scan_index': round(scans / pos * 100, 1) if pos else 0.0,
        })


class BranchListAPIView(APIView):
    """
    GET /api/v1/analytics/branches/

    Returns all active branches for the branch-filter UI.
    """

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(services.get_branches_list())


class SendSegmentBroadcastAPIView(APIView):
    """
    POST /api/v1/analytics/rf/send-broadcast/

    Creates a Broadcast + BroadcastSend per branch and sends VK messages.

    Accepts both JSON and multipart form data (for image upload).

    Body:
      segment_id   — RFSegment PK (опционально; без него — рассылка всем оцифрованным)
      message_text — broadcast text (required, max 4096 chars)
      mode         — restaurant | delivery (default: restaurant)
      branch_ids   — comma-separated Branch PKs (omit = все активные)
      image        — image file (optional, multipart only)

    Если segment_id передан — Broadcast привязывается к указанному RFSegment
    (audience_type=ALL + rf_segments=[segment]). Если segment_id опущен —
    создаётся Broadcast БЕЗ rf_segments (audience_type=ALL → все оцифрованные
    в данной точке).
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT})
    def post(self, request):
        import json
        import random as _random

        from apps.tenant.analytics.models import RFSegment
        from apps.tenant.branch.models import Branch, ClientBranch
        from apps.tenant.senler.models import Broadcast, AudienceType, GenderFilter
        from apps.tenant.senler.services import create_send, run_broadcast

        segment_id    = request.data.get('segment_id')
        message_text  = (request.data.get('message_text') or '').strip()
        mode          = request.data.get('mode', 'restaurant')
        branch_ids    = request.data.get('branch_ids', '')
        gender_filter = request.data.get('gender_filter', 'all')
        variants_raw  = request.data.get('variants', '')
        image_file    = request.FILES.get('image')

        if gender_filter not in {GenderFilter.ALL, GenderFilter.MALE, GenderFilter.FEMALE}:
            gender_filter = GenderFilter.ALL

        # Parse variants. Если не пришли — используем message_text как единственный вариант.
        variants = []
        if variants_raw:
            try:
                parsed = json.loads(variants_raw)
                for v in parsed:
                    pct  = int(v.get('percent', 0))
                    txt  = (v.get('message_text') or '').strip()
                    variants.append({'percent': pct, 'text': txt})
            except (ValueError, TypeError, AttributeError):
                return Response({'error': 'Некорректный формат variants'}, status=status.HTTP_400_BAD_REQUEST)
        if not variants:
            variants = [{'percent': 100, 'text': message_text}]

        for i, v in enumerate(variants):
            if not v['text']:
                return Response({'error': f'Вариант {i + 1}: текст не может быть пустым'}, status=status.HTTP_400_BAD_REQUEST)
            if len(v['text']) > 4096:
                return Response({'error': f'Вариант {i + 1}: превышен лимит 4096 символов'}, status=status.HTTP_400_BAD_REQUEST)
        if len(variants) > 1 and sum(v['percent'] for v in variants) != 100:
            return Response({'error': 'Сумма процентов вариантов должна быть 100'}, status=status.HTTP_400_BAD_REQUEST)

        # Optional segment lookup — без segment_id рассылаем всем оцифрованным.
        segment = None
        if segment_id:
            try:
                segment = RFSegment.objects.get(pk=segment_id)
            except RFSegment.DoesNotExist:
                return Response({'error': 'Сегмент не найден'}, status=status.HTTP_404_NOT_FOUND)

        # Resolve target branches
        if branch_ids:
            try:
                ids = [int(x) for x in str(branch_ids).split(',') if x.strip()]
                branches = Branch.objects.filter(is_active=True, pk__in=ids)
            except ValueError:
                branches = Branch.objects.filter(is_active=True)
        else:
            branches = Branch.objects.filter(is_active=True)

        if not branches.exists():
            return Response({'error': 'Нет активных торговых точек'}, status=status.HTTP_400_BAD_REQUEST)

        broadcast_label = (
            f'RF: {segment.emoji} {segment.name} ({segment.code})' if segment
            else 'Рассылка всем оцифрованным гостям'
        )
        triggered_by = getattr(request.user, 'username', 'api')

        results = []
        # run_broadcast откладывается: сначала создаём все BroadcastSend,
        # потом решаем sync (мало получателей) или один серийный celery-таск
        # (много — иначе синхронный запрос упрётся в таймаут gunicorn/nginx).
        pending: list = []

        if len(variants) == 1:
            # Один текст. Дедупликация: гость, привязанный к нескольким выбранным
            # точкам, получает сообщение только от первой по порядку точки.
            # Поэтому идём по точкам по возрастанию pk и складываем уже-увиденных vk_id.
            single_text = variants[0]['text']
            seen_vk_ids: set[int] = set()

            for branch in branches.order_by('pk'):
                cb_qs = ClientBranch.objects.filter(
                    branch=branch,
                    is_employee=False,
                    client__is_active=True,
                    client__vk_id__isnull=False,
                ).select_related('client')
                if gender_filter != GenderFilter.ALL:
                    cb_qs = cb_qs.filter(client__gender=gender_filter)
                if segment is not None:
                    cb_qs = cb_qs.filter(client__rf_score__segment=segment)
                cb_qs = cb_qs.exclude(client__vk_id__in=seen_vk_ids)

                cb_list = list(cb_qs)
                if not cb_list:
                    results.append({
                        'branch': branch.name, 'branch_id': branch.pk,
                        'status': 'skipped', 'sent': 0, 'failed': 0, 'skipped': 0,
                        'total': 0,
                        'error': 'Все подходящие гости уже включены в рассылку по другим точкам',
                    })
                    continue
                seen_vk_ids.update(cb.client.vk_id for cb in cb_list if cb.client.vk_id)

                broadcast = Broadcast.objects.create(
                    branch=branch,
                    name=broadcast_label,
                    message_text=single_text,
                    audience_type=AudienceType.SPECIFIC,
                    gender_filter=gender_filter,
                    image=image_file if image_file else None,
                )
                broadcast.specific_clients.set([cb.pk for cb in cb_list])

                send = create_send(broadcast, triggered_by=triggered_by, trigger_type='manual')
                pending.append({
                    'send': send, 'count': len(cb_list),
                    'meta': {'branch': branch.name, 'branch_id': branch.pk},
                })
        else:
            # A/B/% сплит — резолвим аудиторию per-branch (с дедупом по vk_id между
            # точками), режем, и каждому куску создаём отдельный Broadcast(SPECIFIC).
            seen_vk_ids_split: set[int] = set()
            for branch in branches.order_by('pk'):
                cb_qs = ClientBranch.objects.filter(
                    branch=branch,
                    is_employee=False,
                    client__is_active=True,
                    client__vk_id__isnull=False,
                ).select_related('client')
                if gender_filter != GenderFilter.ALL:
                    cb_qs = cb_qs.filter(client__gender=gender_filter)
                if segment is not None:
                    cb_qs = cb_qs.filter(client__rf_score__segment=segment)
                cb_qs = cb_qs.exclude(client__vk_id__in=seen_vk_ids_split)

                cb_objs = list(cb_qs)
                seen_vk_ids_split.update(cb.client.vk_id for cb in cb_objs if cb.client.vk_id)
                cb_list = [cb.pk for cb in cb_objs]
                _random.shuffle(cb_list)
                n = len(cb_list)

                cursor = 0
                for i, v in enumerate(variants):
                    if i == len(variants) - 1:
                        chunk = cb_list[cursor:]                                # хвост
                    else:
                        size  = round(n * v['percent'] / 100)
                        chunk = cb_list[cursor:cursor + size]
                        cursor += size

                    variant_label = f'{broadcast_label} — вариант {i + 1} ({v["percent"]}%)'
                    broadcast = Broadcast.objects.create(
                        branch=branch,
                        name=variant_label,
                        message_text=v['text'],
                        audience_type=AudienceType.SPECIFIC,
                        gender_filter=gender_filter,
                        image=image_file if image_file else None,
                    )
                    if chunk:
                        broadcast.specific_clients.set(chunk)

                    send = create_send(broadcast, triggered_by=triggered_by, trigger_type='manual')
                    pending.append({
                        'send': send, 'count': len(chunk),
                        'meta': {
                            'branch': branch.name, 'branch_id': branch.pk,
                            'variant': f'#{i + 1} ({v["percent"]}%)',
                        },
                    })

        segment_label = f'{segment.emoji} {segment.name}' if segment else 'Все оцифрованные гости'
        total_recipients = sum(p['count'] for p in pending)

        # Порог: мало получателей — шлём синхронно (мгновенные счётчики,
        # UX без изменений). Много — один серийный celery-таск (внутри
        # run_broadcast sleep(0.05)=≤20 msg/s; один таск на запрос не даёт
        # параллелизмом превысить лимит VK). Так синхронный HTTP-запрос
        # не упирается в таймаут на больших сегментах.
        SYNC_RECIPIENT_LIMIT = 30

        if pending and total_recipients > SYNC_RECIPIENT_LIMIT:
            from django.db import connection
            from apps.tenant.senler.tasks import run_broadcast_task

            send_ids = [p['send'].id for p in pending]
            run_broadcast_task.delay(connection.schema_name, send_ids)

            queued = [
                {**p['meta'], 'status': 'queued', 'total': p['count']}
                for p in pending
            ]
            return Response({
                'ok':               True,
                'queued':           True,
                'segment':          segment_label,
                'results':          queued + results,
                'total_recipients': total_recipients,
            })

        # Sync-путь: поведение и форма ответа идентичны прежним.
        for p in pending:
            send = p['send']
            try:
                run_broadcast(send)
                send.refresh_from_db()
                results.append({
                    **p['meta'],
                    'status': send.status, 'sent': send.sent_count,
                    'failed': send.failed_count, 'skipped': send.skipped_count,
                    'total': send.recipients_count,
                    'error': send.error_message or '',
                })
            except Exception as e:
                results.append({**p['meta'], 'status': 'failed', 'error': str(e)})

        total_sent = sum(r.get('sent', 0) for r in results)
        return Response({
            'ok':         True,
            'segment':    segment_label,
            'results':    results,
            'total_sent': total_sent,
        })


class GenerateBroadcastTextAPIView(APIView):
    """
    POST /api/v1/analytics/rf/generate-broadcast-text/

    Uses Claude AI to generate a broadcast message.

    Body (JSON):
      segment_id — RFSegment PK (опционально). Если передан — текст пишется
                   с подсказкой по сегменту. Без него — общий текст для всех
                   оцифрованных гостей.
    """

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT})
    def post(self, request):
        import json as _json
        from django.conf import settings as _settings

        segment_id = request.data.get('segment_id')

        segment = None
        if segment_id:
            from apps.tenant.analytics.models import RFSegment
            try:
                segment = RFSegment.objects.get(pk=segment_id)
            except RFSegment.DoesNotExist:
                return Response({'error': 'Сегмент не найден'}, status=status.HTTP_404_NOT_FOUND)

        # Get tenant/company name for context
        try:
            from django.db import connection
            company_name = getattr(connection.tenant, 'name', 'наше кафе')
        except Exception:
            company_name = 'наше кафе'

        api_key = getattr(_settings, 'ANTHROPIC_API_KEY', None)
        if not api_key:
            return Response(
                {'error': 'ANTHROPIC_API_KEY не настроен'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        system_prompt = (
            'Ты — маркетолог ресторана/кафе. Пишешь VK-рассылки для гостей.\n'
            'Правила:\n'
            '- Пиши на русском, дружелюбно.\n'
            '- Если пользователь дал конкретное задание — это ГЛАВНОЕ. Пиши '
            'строго про то, что он просит: повод, адрес, скидку, дату и т.д. '
            'Не подменяй его задание общим приветствием.\n'
            '- База знаний — это справка о заведении (тон общения, факты, '
            'адреса, действующие акции). Используй её как фон, но НИКОГДА не '
            'копируй из неё готовые тексты рассылок и не бери оттуда повод, '
            'если пользователь попросил другой.\n'
            '- Не используй markdown, HTML, заглавные буквы целыми словами.\n'
            '- Не используй скобки и эмодзи чаще 1-2 раз.\n'
            '- Текст должен быть готов к отправке — без плейсхолдеров.\n'
            '- Длина: до 2000 символов. Если пользователь явно просит написать '
            'длиннее — до 4000 символов.\n'
            '- Верни ТОЛЬКО текст рассылки, без пояснений.'
        )

        # Подмешиваем базу знаний тенанта — как справку, не как шаблон.
        from apps.tenant.analytics.ai_service import _get_knowledge_base_text
        kb_text = _get_knowledge_base_text()
        if kb_text:
            system_prompt += (
                '\n\n--- Справка о заведении из базы знаний ---\n'
                '(используй только для тона, фактов, адресов и действующих '
                'акций; НЕ копируй отсюда готовые тексты рассылок)\n'
                + kb_text
            )

        # Черновик/пожелания пользователя из поля рассылки.
        draft = (request.data.get('draft') or '').strip()

        if draft:
            user_message = (
                f'Кафе: {company_name}\n\n'
                f'ЗАДАНИЕ ОТ ПОЛЬЗОВАТЕЛЯ — выполни именно его:\n{draft}\n\n'
                f'Напиши на основе этого задания готовый текст VK-рассылки. '
                f'Пиши строго про то, что указано в задании; не заменяй его '
                f'общим приветствием и не бери повод из базы знаний.'
            )
        elif segment is not None:
            code = segment.code
            std = services._STANDARD_SEGMENT_DATA.get(code, {})
            hint = std.get('hint', segment.hint or segment.strategy or '')
            user_message = (
                f'Кафе: {company_name}\n'
                f'Сегмент гостей: {segment.name} ({segment.code})\n'
                f'Подсказка по сегменту:\n{hint}\n\n'
                f'Напиши короткое VK-сообщение для этого сегмента.'
            )
        else:
            user_message = (
                f'Кафе: {company_name}\n'
                f'Аудитория: ВСЕ оцифрованные гости заведения (любая давность и частота визитов).\n'
                f'Напиши универсальное короткое VK-сообщение — приветственное, '
                f'мотивирующее заглянуть в кафе, без привязки к конкретному поводу.'
            )

        try:
            import os
            import anthropic

            proxy_url = os.getenv('AI_PROXY_URL', '')
            client = (
                anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
                if proxy_url
                else anthropic.Anthropic(api_key=api_key)
            )

            message = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=2048,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_message}],
            )
            generated_text = message.content[0].text.strip()

            return Response({'text': generated_text})

        except Exception as e:
            return Response(
                {'error': f'Ошибка генерации: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class GenerateReportCommentAPIView(APIView):
    """
    POST /api/v1/analytics/report/generate-comment/

    Uses Claude AI to generate a manager comment for a report section.

    Body (JSON):
      section_num   — section number (1-11)
      section_title — section title
      metrics_json  — JSON string of section metrics data
    """

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT})
    def post(self, request):
        import json as _json
        from django.conf import settings as _settings

        section_num   = request.data.get('section_num', '')
        section_title = request.data.get('section_title', '')
        metrics_json  = request.data.get('metrics_json', '{}')

        try:
            from django.db import connection
            company_name = getattr(connection.tenant, 'name', 'кафе')
        except Exception:
            company_name = 'кафе'

        api_key = getattr(_settings, 'ANTHROPIC_API_KEY', None)
        if not api_key:
            return Response(
                {'error': 'ANTHROPIC_API_KEY не настроен'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        system_prompt = (
            'Ты — менеджер системы лояльности ресторана/кафе. Пишешь короткий '
            'аналитический комментарий к разделу отчёта.\n'
            'Правила:\n'
            '- Пиши на русском, профессионально, коротко (2-3 предложения).\n'
            '- Опирайся ТОЛЬКО на переданные ниже числа этого раздела. Не '
            'выдумывай данные, акции, события и факты, которых нет в цифрах.\n'
            '- Сделай конкретный вывод именно по этим числам: что выросло или '
            'упало, что это значит и что стоит предпринять.\n'
            '- Не используй markdown, HTML.\n'
            '- Для секций 10 и 11 пиши 3 пункта через символ новой строки, каждый начиная с «•».\n'
            '- Верни ТОЛЬКО текст комментария, без пояснений.'
        )

        # База знаний (инструкции по рассылкам/отзывам) для отчётов не нужна —
        # она только сбивает модель. Комментарий строится строго по цифрам.

        user_message = (
            f'Кафе: {company_name}\n'
            f'Раздел отчёта #{section_num}: {section_title}\n'
            f'Данные раздела: {metrics_json}\n\n'
            f'Напиши короткий аналитический комментарий менеджера для этого раздела отчёта.'
        )

        try:
            import os
            import anthropic

            proxy_url = os.getenv('AI_PROXY_URL', '')
            client = (
                anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
                if proxy_url
                else anthropic.Anthropic(api_key=api_key)
            )

            message = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=512,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_message}],
            )
            generated_text = message.content[0].text.strip()
            return Response({'text': generated_text})

        except Exception as e:
            return Response(
                {'error': f'Ошибка генерации: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
