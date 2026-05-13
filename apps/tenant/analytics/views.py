"""
Analytics HTML views — render dashboard pages inside the admin shell.
"""
import json
from datetime import date, timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views import View
from django.utils import timezone

from django.db.models import Avg, Count, Q, Case, When, IntegerField

from apps.tenant.branch.models import Branch, TestimonialConversation, TestimonialMessage
from apps.tenant.analytics.api.services import (
    get_general_stats,
    get_chart_data,
    get_rf_stats,
    get_migration_history,
)

PERIOD_CHOICES = [
    ('today', 'Сегодня'),
    ('7d',    '7 дней'),
    ('30d',   '30 дней'),
    ('90d',   '90 дней'),
    ('year',  'За год'),
    ('all',   'За всё время'),
]


def _period_qs(active_period: str, start: date, end: date) -> str:
    """Return query-string fragment that preserves the current period across navigations."""
    if active_period == 'custom':
        return f'start={start.isoformat()}&end={end.isoformat()}'
    return f'period={active_period}'


def _parse_period(request) -> tuple[date, date, str]:
    today  = date.today()
    preset = request.GET.get('period', '30d')
    s, e   = request.GET.get('start'), request.GET.get('end')

    if s and e:
        try:
            return date.fromisoformat(s), date.fromisoformat(e), 'custom'
        except ValueError:
            pass

    periods = {
        'today': (today, today),
        '7d':    (today - timedelta(days=6),  today),
        '30d':   (today - timedelta(days=29), today),
        '90d':   (today - timedelta(days=89), today),
        'year':  (today.replace(month=1, day=1), today),
        'all':   (date(2000, 1, 1), today),
    }
    start, end = periods.get(preset, periods['30d'])
    return start, end, preset


def _parse_branch_ids(request) -> list[int]:
    raw = request.GET.get('branches', '')
    if not raw:
        return []
    try:
        return [int(x) for x in raw.split(',') if x.strip()]
    except ValueError:
        return []


def _branches_context(request):
    branches   = list(Branch.objects.filter(is_active=True).values('id', 'name').order_by('name'))
    branch_ids = _parse_branch_ids(request)
    active     = [b for b in branches if b['id'] in branch_ids] if branch_ids else []
    return branches, branch_ids or None, active


@method_decorator(staff_member_required, name='dispatch')
class GeneralStatsView(View):
    template_name = 'analytics/general_stats.html'

    def get(self, request):
        start, end, active_period = _parse_period(request)
        branches, branch_ids, active_branches = _branches_context(request)

        stats  = get_general_stats(branch_ids, start, end, skip_slow=True)
        charts = get_chart_data(branch_ids, start, end)

        # Reviews summary for the VK reviews widget
        _reviews_qs = TestimonialConversation.objects.filter(
            last_message_at__date__gte=start,
            last_message_at__date__lte=end,
        )
        if branch_ids:
            _reviews_qs = _reviews_qs.filter(branch_id__in=branch_ids)
        _sc = {
            row['sentiment']: row['cnt']
            for row in _reviews_qs.values('sentiment').annotate(cnt=Count('id'))
        }
        S = TestimonialConversation.Sentiment
        reviews_total = _reviews_qs.count()
        reviews_summary = {
            'positive':  _sc.get(S.POSITIVE, 0),
            'negative':  _sc.get(S.NEGATIVE, 0),
            'partial':   _sc.get(S.PARTIALLY_NEGATIVE, 0),
            'neutral':   _sc.get(S.NEUTRAL, 0),
            'spam':      _sc.get(S.SPAM, 0),
            'waiting':   _sc.get(S.WAITING, 0),
            'total':     reviews_total,
        }

        # Add extra chart data that depends on reviews_summary and stats
        charts['reviews_sentiment'] = {
            'positive': _sc.get(S.POSITIVE, 0),
            'negative': _sc.get(S.NEGATIVE, 0),
            'partial':  _sc.get(S.PARTIALLY_NEGATIVE, 0),
        }
        charts['reviews_ratio'] = {
            'left':     reviews_total,
            'not_left': max(0, stats['qr_scans'] - reviews_total),
        }
        charts['sources'] = {
            'from_cafe':    stats['qr_scans'],
            'from_stories': stats['stories_referrals'],
        }

        context = {
            'title':             'Статистика',
            'stats':             stats,
            'charts_json':       json.dumps(charts),
            'reviews_summary':   reviews_summary,
            'branches':          branches,
            'active_branch_ids': branch_ids or [],
            'active_branches':   active_branches,
            'active_period':     active_period,
            'period_choices':    PERIOD_CHOICES,
            'period_qs':         _period_qs(active_period, start, end),
            'start':             start.isoformat(),
            'end':               end.isoformat(),
            'start_display':     start.strftime('%d.%m.%Y'),
            'end_display':       end.strftime('%d.%m.%Y'),
        }
        return render(request, self.template_name, context)


@method_decorator(staff_member_required, name='dispatch')
class RFAnalysisView(View):
    template_name = 'analytics/rf_analysis.html'

    def get(self, request):
        from apps.tenant.analytics.models import RFSettings

        branches, branch_ids, active_branches = _branches_context(request)
        mode = request.GET.get('mode', 'restaurant')  # restaurant | delivery
        start, end, active_period = _parse_period(request)

        rf_restaurant = get_rf_stats(branch_ids, mode='restaurant', start_date=start, end_date=end)
        rf_delivery   = get_rf_stats(branch_ids, mode='delivery',   start_date=start, end_date=end)

        period_days = (end - start).days + 1

        # ── Текущие пороги для выбранной области (для показа в подсказке) ──
        settings_obj, thresholds, thresholds_source = RFSettings.resolve_for_scope(branch_ids)
        scope_label = (
            settings_obj.scope_label if settings_obj else
            ('Все точки' if not branch_ids else 'выбранные точки')
        )
        thresholds_source_label = {
            'branch':  'настройки выбранной точки',
            'global':  'общие настройки «Все точки»',
            'default': 'дефолтные значения (настроек ещё нет)',
        }.get(thresholds_source, thresholds_source)

        # Для удобства: ссылка на редактирование настроек выбранной области.
        # Если объект существует — ведём на change-страницу; иначе — на add.
        # Используем имя именно tenant_admin (analytics живёт в тенант-схеме),
        # чтобы избежать NoReverseMatch при двух AdminSite (tenant_admin + public_admin).
        from apps.shared.config.admin_sites import tenant_admin
        _admin_ns = tenant_admin.name
        if settings_obj is not None:
            settings_edit_url = reverse(
                f'{_admin_ns}:analytics_rfsettings_change', args=[settings_obj.pk],
            )
        else:
            settings_edit_url = reverse(f'{_admin_ns}:analytics_rfsettings_add')

        context = {
            'title':              'RF-анализ',
            'branches':           branches,
            'active_branch_ids':  branch_ids or [],
            'active_branches':    active_branches,
            'active_mode':        mode,
            'updated_at':         timezone.localdate().strftime('%d.%m.%Y'),
            'active_period':      active_period,
            'period_choices':     PERIOD_CHOICES,
            'period_qs':          _period_qs(active_period, start, end),
            'start':              start.isoformat(),
            'end':                end.isoformat(),
            'start_display':      start.strftime('%d.%m.%Y'),
            'end_display':        end.strftime('%d.%m.%Y'),
            'period_days':        period_days,
            # Restaurant
            'rf':                 rf_restaurant,
            'rf_json':            json.dumps(rf_restaurant['matrix']),
            # Delivery
            'rf_delivery':        rf_delivery,
            'rf_delivery_json':   json.dumps(rf_delivery['matrix']),
            # Thresholds info (для информационной панели в шаблоне)
            'thresholds':              thresholds,
            'thresholds_source':       thresholds_source,
            'thresholds_source_label': thresholds_source_label,
            'thresholds_scope_label':  scope_label,
            'thresholds_edit_url':     settings_edit_url,
        }
        return render(request, self.template_name, context)


@method_decorator(staff_member_required, name='dispatch')
class RFMigrationView(View):
    template_name = 'analytics/rf_migration.html'

    def get(self, request):
        branches, branch_ids, active_branches = _branches_context(request)
        mode         = request.GET.get('mode', 'restaurant')
        segment_code = request.GET.get('segment', '') or None
        days_raw     = request.GET.get('days', '30')
        try:
            days = int(days_raw)
        except ValueError:
            days = 30

        data = get_migration_history(branch_ids, days=days, mode=mode, segment_code=segment_code)

        context = {
            'title':             'История миграции гостей',
            'branches':          branches,
            'active_branch_ids': branch_ids or [],
            'active_branches':   active_branches,
            'active_mode':       mode,
            'active_days':       days,
            'active_segment':    segment_code or '',
            'flows':             data['flows'],
            'effectiveness':     data['effectiveness'],
            'all_segments':      data['all_segments'],
            'flows_json':        json.dumps(data['flows']),
            'days_choices':      [('7', '7 дней'), ('30', '30 дней'), ('90', '90 дней'), ('365', 'Год')],
            'updated_at':        timezone.localdate().strftime('%d.%m.%Y'),
        }
        return render(request, self.template_name, context)


@method_decorator(staff_member_required, name='dispatch')
class ReviewsAnalyticsView(View):
    template_name = 'analytics/reviews.html'

    def get(self, request):
        start, end, active_period = _parse_period(request)
        branches, branch_ids, active_branches = _branches_context(request)

        qs = TestimonialConversation.objects.filter(
            last_message_at__date__gte=start,
            last_message_at__date__lte=end,
        )
        if branch_ids:
            qs = qs.filter(branch_id__in=branch_ids)

        total = qs.count()

        sentiment_counts = {
            row['sentiment']: row['cnt']
            for row in qs.values('sentiment').annotate(cnt=Count('id'))
        }

        msg_qs = TestimonialMessage.objects.filter(
            conversation__in=qs,
            source=TestimonialMessage.Source.APP,
            rating__isnull=False,
        )
        avg_rating = msg_qs.aggregate(avg=Avg('rating'))['avg']
        _rating_map = {
            row['rating']: row['cnt']
            for row in msg_qs.values('rating').annotate(cnt=Count('id'))
        }
        _max_rc = max(_rating_map.values(), default=1) or 1
        rating_list = [
            {
                'star':  s,
                'count': _rating_map.get(s, 0),
                'pct':   round(_rating_map.get(s, 0) / _max_rc * 100),
                'stars': '★' * s + '☆' * (5 - s),
            }
            for s in range(5, 0, -1)
        ]

        source_counts = {
            row['source']: row['cnt']
            for row in TestimonialMessage.objects.filter(
                conversation__in=qs,
            ).exclude(source=TestimonialMessage.Source.ADMIN_REPLY).values('source').annotate(cnt=Count('conversation', distinct=True))
        }

        recent = (
            qs.select_related('branch', 'client__client')
            .order_by('-last_message_at')[:20]
        )

        Sentiment = TestimonialConversation.Sentiment
        sentiment_labels = {
            Sentiment.POSITIVE:           ('Позитивные',           '#16a34a'),
            Sentiment.NEGATIVE:           ('Негативные',           '#dc2626'),
            Sentiment.PARTIALLY_NEGATIVE: ('Частично негативные',  '#f59e0b'),
            Sentiment.NEUTRAL:            ('Нейтральные',          '#6b7280'),
            Sentiment.SPAM:               ('Спам',                 '#9ca3af'),
            Sentiment.WAITING:            ('Ожидают анализа',      '#3b82f6'),
        }

        sentiment_data = [
            {
                'key':   key,
                'label': label,
                'color': color,
                'count': sentiment_counts.get(key, 0),
            }
            for key, (label, color) in sentiment_labels.items()
        ]

        context = {
            'title':             'Анализ отзывов',
            'total':             total,
            'avg_rating':        round(avg_rating, 1) if avg_rating else None,
            'rating_list':       rating_list,
            'source_counts':     source_counts,
            'sentiment_data':    sentiment_data,
            'sentiment_json':    json.dumps([
                {'label': d['label'], 'color': d['color'], 'count': d['count']}
                for d in sentiment_data
            ]),
            'recent':            recent,
            'branches':          branches,
            'active_branch_ids': branch_ids or [],
            'active_branches':   active_branches,
            'active_period':     active_period,
            'period_choices':    PERIOD_CHOICES,
            'period_qs':         _period_qs(active_period, start, end),
            'start':             start.isoformat(),
            'end':               end.isoformat(),
            'start_display':     start.strftime('%d.%m.%Y'),
            'end_display':       end.strftime('%d.%m.%Y'),
        }
        return render(request, self.template_name, context)


# ── Stat metric labels (for detail view title) ────────────────────────────────

_METRIC_LABELS = {
    'qr_scans':                  'Отсканировали QR-код',
    'total_vk_subscribers':      'Подписчики ВКонтакте',
    'new_group_with_gift':       'Новые в группе с первым подарком',
    'repeat_game_players':       'Вернулись и сыграли повторно',
    'coin_purchasers':           'Купили подарки за баллы',
    'new_community_subscribers': 'Подписались в сообщество ВК',
    'new_newsletter_subscribers': 'Подписались на рассылку ВК',
    'first_gift_receivers':      'Получили первый подарок',
    'gift_activators':           'Активировали подарок',
    'birthday_celebrants':       'Пришли на день рождения',
    'vk_stories_publishers':     'Опубликовали истории в ВК',
}


@method_decorator(staff_member_required, name='dispatch')
class StatsDetailView(View):
    template_name = 'analytics/stats_detail.html'

    def get(self, request):
        from apps.tenant.analytics.api.services import get_stat_clients

        start, end, active_period = _parse_period(request)
        branches, branch_ids, active_branches = _branches_context(request)
        metric = request.GET.get('metric', '')

        clients = get_stat_clients(metric, branch_ids, start, end)

        context = {
            'title':             _METRIC_LABELS.get(metric, metric),
            'metric':            metric,
            'clients':           clients,
            'total':             clients.count(),
            'branches':          branches,
            'active_branch_ids': branch_ids or [],
            'active_branches':   active_branches,
            'active_period':     active_period,
            'period_choices':    PERIOD_CHOICES,
            'period_qs':         _period_qs(active_period, start, end),
            'start':             start.isoformat(),
            'end':               end.isoformat(),
            'start_display':     start.strftime('%d.%m.%Y'),
            'end_display':       end.strftime('%d.%m.%Y'),
        }
        return render(request, self.template_name, context)


@method_decorator(staff_member_required, name='dispatch')
class ReviewsReplyView(View):
    def post(self, request):
        import json as _json
        from django.http import JsonResponse
        from apps.tenant.branch.api.services import send_vk_reply
        from apps.tenant.branch.models import TestimonialConversation

        try:
            body       = _json.loads(request.body)
            conv_id    = int(body.get('conv_id', 0))
            reply_text = (body.get('reply_text') or '').strip()
        except (ValueError, TypeError, _json.JSONDecodeError):
            return JsonResponse({'ok': False, 'error': 'Неверный запрос'}, status=400)

        if not reply_text:
            return JsonResponse({'ok': False, 'error': 'Текст не может быть пустым'}, status=400)

        try:
            conv = TestimonialConversation.objects.get(pk=conv_id)
        except TestimonialConversation.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'Диалог не найден'}, status=404)

        try:
            send_vk_reply(conv, reply_text)
        except ValueError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Ошибка VK API: {e}'}, status=500)

        return JsonResponse({'ok': True})


@method_decorator(staff_member_required, name='dispatch')
class ReviewsAIReplyView(View):
    def post(self, request):
        import json as _json
        from django.http import JsonResponse
        from django.conf import settings
        from apps.tenant.branch.models import TestimonialConversation

        try:
            body    = _json.loads(request.body)
            conv_id = int(body.get('conv_id', 0))
            draft   = str(body.get('draft', '')).strip()
        except (ValueError, TypeError, _json.JSONDecodeError):
            return JsonResponse({'error': 'Неверный запрос'}, status=400)

        try:
            conv = TestimonialConversation.objects.prefetch_related('messages').get(pk=conv_id)
        except TestimonialConversation.DoesNotExist:
            return JsonResponse({'error': 'Диалог не найден'}, status=404)

        lines = []
        for msg in conv.messages.order_by('created_at'):
            if msg.source == 'ADMIN_REPLY':
                role = 'Администратор'
            elif msg.source == 'APP':
                role = 'Гость (приложение)'
            else:
                role = 'Гость (ВКонтакте)'
            lines.append(f'{role}: {msg.text}')
        conv_text = '\n'.join(lines) or 'Нет сообщений'

        api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
        if not api_key:
            return JsonResponse({'error': 'AI не настроен (ANTHROPIC_API_KEY)'}, status=500)

        try:
            import os, anthropic
            proxy_url = os.getenv('AI_PROXY_URL', '')
            if proxy_url:
                ai_client = anthropic.Anthropic(
                    api_key=api_key,
                    base_url=proxy_url,
                )
            else:
                ai_client = anthropic.Anthropic(api_key=api_key)
            if draft:
                system_prompt = (
                    'Ты — помощник администратора ресторана/кафе. '
                    'Администратор написал черновик ответа гостю. '
                    'Улучши этот черновик: сделай его более вежливым, профессиональным и грамотным, '
                    'сохраняя смысл и основную идею администратора. '
                    'Ответ должен быть коротким (2-4 предложения), дружелюбным и по существу. '
                    'Возвращай только текст ответа без кавычек и пояснений.'
                )
                user_content = f'История диалога:\n\n{conv_text}\n\nЧерновик ответа администратора:\n{draft}'
            else:
                system_prompt = (
                    'Ты — помощник администратора ресторана/кафе. '
                    'Составь вежливый и профессиональный ответ на отзыв гостя от имени заведения. '
                    'Ответ должен быть коротким (2-4 предложения), дружелюбным и по существу. '
                    'Возвращай только текст ответа без кавычек и пояснений.'
                )
                user_content = f'История диалога:\n\n{conv_text}'

            msg = ai_client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=512,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_content}],
            )
            suggestion = msg.content[0].text.strip()
        except Exception as e:
            return JsonResponse({'error': f'Ошибка AI: {e}'}, status=500)

        return JsonResponse({'text': suggestion})


@method_decorator(staff_member_required, name='dispatch')
class ReviewsDetailView(View):
    template_name = 'analytics/reviews_detail.html'

    def get(self, request):
        start, end, active_period = _parse_period(request)
        branches, branch_ids, active_branches = _branches_context(request)
        sentiment = request.GET.get('sentiment', '')

        qs = TestimonialConversation.objects.filter(
            last_message_at__date__gte=start,
            last_message_at__date__lte=end,
        ).select_related('branch', 'client__client').prefetch_related('messages')
        if branch_ids:
            qs = qs.filter(branch_id__in=branch_ids)
        if sentiment:
            qs = qs.filter(sentiment=sentiment)
        qs = qs.annotate(
            needs_reply=Case(
                When(has_unread=True, is_replied=False, then=0),
                default=1,
                output_field=IntegerField(),
            )
        ).order_by('needs_reply', '-last_message_at')

        _SENTIMENT_LABELS = {
            'POSITIVE':           'Позитивные',
            'NEGATIVE':           'Негативные',
            'PARTIALLY_NEGATIVE': 'Частично негативные',
            'NEUTRAL':            'Нейтральные',
            'SPAM':               'Спам',
            'WAITING':            'Ожидают анализа',
        }
        _SENTIMENT_COLORS = {
            'POSITIVE':           '#16a34a',
            'NEGATIVE':           '#dc2626',
            'PARTIALLY_NEGATIVE': '#f59e0b',
            'NEUTRAL':            '#6b7280',
            'SPAM':               '#9ca3af',
            'WAITING':            '#3b82f6',
        }

        context = {
            'title':             _SENTIMENT_LABELS.get(sentiment, 'Все отзывы'),
            'sentiment':         sentiment,
            'sentiment_color':   _SENTIMENT_COLORS.get(sentiment, '#6b7280'),
            'conversations':     qs,
            'total':             qs.count(),
            'branches':          branches,
            'active_branch_ids': branch_ids or [],
            'active_branches':   active_branches,
            'active_period':     active_period,
            'period_choices':    PERIOD_CHOICES,
            'period_qs':         _period_qs(active_period, start, end),
            'start':             start.isoformat(),
            'end':               end.isoformat(),
            'start_display':     start.strftime('%d.%m.%Y'),
            'end_display':       end.strftime('%d.%m.%Y'),
        }
        return render(request, self.template_name, context)


# ── Segment actions (from RF dashboard) ──────────────────────────────────────

@method_decorator(staff_member_required, name='dispatch')
class SegmentExportSenlerView(View):
    """
    GET /analytics/rf/segment/<segment_id>/export-senler/
    Downloads .txt with VK IDs (one per line) for Senler import.
    """

    def get(self, request, segment_id):
        from apps.tenant.analytics.models import GuestRFScore, GuestRFScoreDelivery, RFSegment

        segment = RFSegment.objects.get(pk=segment_id)
        mode = request.GET.get('mode', 'restaurant')

        ScoreModel = GuestRFScoreDelivery if mode == 'delivery' else GuestRFScore
        qs = ScoreModel.objects.filter(segment=segment)

        branch_ids = request.GET.get('branches', '')
        if branch_ids:
            try:
                ids = [int(x) for x in branch_ids.split(',') if x.strip()]
                qs = qs.filter(client__branch_id__in=ids)
            except ValueError:
                pass

        vk_ids = qs.values_list('client__client__vk_id', flat=True)
        lines = [str(vk_id) for vk_id in vk_ids if vk_id]
        content = '\n'.join(lines)

        # Use ASCII-safe filename + filename* for proper Cyrillic support
        from urllib.parse import quote
        safe_name = f'senler_{segment.code}.txt'
        full_name = f'senler_{segment.code}_{segment.name}.txt'
        response = HttpResponse(content, content_type='text/plain; charset=utf-8')
        response['Content-Disposition'] = (
            f'attachment; filename="{safe_name}"; '
            f"filename*=UTF-8''{quote(full_name)}"
        )
        return response


@method_decorator(staff_member_required, name='dispatch')
class SegmentCreateBroadcastView(View):
    """
    GET /analytics/rf/segment/<segment_id>/create-broadcast/
    Creates a broadcast for the segment and redirects to admin edit page.
    """

    def get(self, request, segment_id):
        from apps.tenant.analytics.models import RFSegment
        from apps.tenant.senler.models import Broadcast

        segment = RFSegment.objects.get(pk=segment_id)

        branch_ids_raw = request.GET.get('branches', '')
        if branch_ids_raw:
            try:
                ids = [int(x) for x in branch_ids_raw.split(',') if x.strip()]
                branches = Branch.objects.filter(is_active=True, pk__in=ids)
            except ValueError:
                branches = Branch.objects.filter(is_active=True)
        else:
            branches = Branch.objects.filter(is_active=True)

        created_ids = []
        for branch in branches:
            broadcast = Broadcast.objects.create(
                branch=branch,
                name=f'Рассылка: {segment.emoji} {segment.name} ({segment.code})',
                message_text='',
                audience_type='all',
            )
            broadcast.rf_segments.set([segment])
            created_ids.append(broadcast.pk)

        if len(created_ids) == 1:
            return HttpResponseRedirect(f'/admin/senler/broadcast/{created_ids[0]}/change/')
        elif created_ids:
            return HttpResponseRedirect('/admin/senler/broadcast/')
        else:
            return HttpResponseRedirect(request.META.get('HTTP_REFERER', '/analytics/rf/'))


@method_decorator(staff_member_required, name='dispatch')
class CreateBroadcastForAllGuestsView(View):
    """
    GET /analytics/rf/broadcast-all/
    Создаёт пустую рассылку всем оцифрованным гостям (audience=ALL,
    rf_segments не привязаны) для каждой выбранной точки и редиректит
    администратора на её редактирование.

    Если выбрана одна точка — редирект на change-страницу созданной рассылки.
    Если несколько/все — редирект на список рассылок.
    """

    def get(self, request):
        from apps.tenant.senler.models import Broadcast

        branch_ids_raw = request.GET.get('branches', '')
        if branch_ids_raw:
            try:
                ids = [int(x) for x in branch_ids_raw.split(',') if x.strip()]
                branches = Branch.objects.filter(is_active=True, pk__in=ids)
            except ValueError:
                branches = Branch.objects.filter(is_active=True)
        else:
            branches = Branch.objects.filter(is_active=True)

        created_ids = []
        for branch in branches:
            broadcast = Broadcast.objects.create(
                branch=branch,
                name='Рассылка всем гостям',
                message_text='',
                audience_type='all',
            )
            created_ids.append(broadcast.pk)

        if len(created_ids) == 1:
            return HttpResponseRedirect(f'/admin/senler/broadcast/{created_ids[0]}/change/')
        if created_ids:
            return HttpResponseRedirect('/admin/senler/broadcast/')
        return HttpResponseRedirect(request.META.get('HTTP_REFERER', '/analytics/rf/'))


@method_decorator(staff_member_required, name='dispatch')
class LoyaltyReportView(View):
    """
    GET /analytics/report/
    Loyalty system report page with all metrics, charts, AI-generated comments, and PDF export.
    """
    template_name = 'analytics/loyalty_report.html'

    def get(self, request):
        start, end, active_period = _parse_period(request)
        branches, branch_ids, active_branches = _branches_context(request)

        stats  = get_general_stats(branch_ids, start, end, skip_slow=False)
        charts = get_chart_data(branch_ids, start, end)

        # Reviews
        _reviews_qs = TestimonialConversation.objects.filter(
            last_message_at__date__gte=start,
            last_message_at__date__lte=end,
        )
        if branch_ids:
            _reviews_qs = _reviews_qs.filter(branch_id__in=branch_ids)
        S = TestimonialConversation.Sentiment
        _sc = {
            row['sentiment']: row['cnt']
            for row in _reviews_qs.values('sentiment').annotate(cnt=Count('id'))
        }
        reviews = {
            'positive':  _sc.get(S.POSITIVE, 0),
            'negative':  _sc.get(S.NEGATIVE, 0),
            'partial':   _sc.get(S.PARTIALLY_NEGATIVE, 0),
            'neutral':   _sc.get(S.NEUTRAL, 0),
            'total':     _reviews_qs.count(),
        }

        # RF data
        rf = get_rf_stats(branch_ids, mode='restaurant')
        rf_summary = rf.get('summary', {})
        rf_matrix  = rf.get('matrix', {})

        # Migration effectiveness
        days_delta = (end - start).days or 30
        from apps.tenant.analytics.api.services import get_migration_effectiveness
        migration = get_migration_effectiveness(branch_ids, days=days_delta, mode='restaurant')

        # Scan index
        scan_index = stats.get('scan_index') or 0.0
        pos_guests = stats.get('pos_guests') or 0

        # ── Sources: cafe vs delivery (непересекающиеся доли qr_scans) ──
        # qr_scans уже включает delivery activators (см. get_qr_scan_count).
        # Поэтому from_cafe = qr_scans − delivery_activators, чтобы
        # проценты в отчёте формировали честные 100% от числа гостей.
        from apps.tenant.analytics.api.services import get_delivery_activators_count
        from_delivery = get_delivery_activators_count(branch_ids, start, end)
        from_cafe     = max(0, stats['qr_scans'] - from_delivery)
        total_sources = stats['qr_scans'] or 1

        # RF segment counts from matrix cells
        segment_counts = {}
        cells = rf_matrix.get('cells', {})
        for key, cell in cells.items():
            seg_name = cell.get('segment_name', '—')
            segment_counts[seg_name] = segment_counts.get(seg_name, 0) + cell.get('count', 0)

        # Build complete report data for template
        report_data = {
            # Section 1: Key metrics
            'new_community':     stats['new_community_subscribers'],
            'new_newsletter':    stats['new_newsletter_subscribers'],
            'total_vk':          stats['total_vk_subscribers'],
            'new_with_gift':     stats['new_group_with_gift'],
            'birthday_sent':     stats['birthday_greetings_sent'],
            'birthday_came':     stats['birthday_celebrants'],
            'repeat_players':    stats['repeat_game_players'],
            'coin_purchasers':   stats['coin_purchasers'],
            'stories_publishers': stats['vk_stories_publishers'],
            'stories_referrals':  stats['stories_referrals'],
            'message_open_rate':  stats['message_open_rate'],
            'message_total_sent': stats['message_total_sent'],
            'message_total_read': stats['message_total_read'],
            'qr_scans':          stats['qr_scans'],
            # Section 3: Scan index
            'pos_guests':        pos_guests,
            'scan_index':        scan_index,
            # Section 4: Sources
            'from_cafe':         from_cafe,
            'from_delivery':     from_delivery,
            'from_cafe_pct':     round(from_cafe / total_sources * 100),
            'from_delivery_pct': round(from_delivery / total_sources * 100),
            # Section 5: Engagement (same as some above)
            # Section 7: Reviews
            'reviews':           reviews,
            # Section 8: RF segments
            'rf_summary':        rf_summary,
            'segment_counts':    segment_counts,
            # Section 9: Migration
            'migration':         migration,
        }

        # Charts JSON for Chart.js
        charts_report = {
            'engagement': {
                'repeat_players':  stats['repeat_game_players'],
                'coin_purchasers': stats['coin_purchasers'],
                'stories':         stats['vk_stories_publishers'],
                'qr_scans':        stats['qr_scans'],
            },
            'reviews_sentiment': {
                'positive': reviews['positive'],
                'neutral':  reviews['neutral'],
                'partial':  reviews['partial'],
                'negative': reviews['negative'],
            },
            'sources': {
                'cafe':     from_cafe,
                'delivery': from_delivery,
            },
            'rf_segments': segment_counts,
            'migration': migration,
            'growth': {
                'new_community':  stats['new_community_subscribers'],
                'new_newsletter': stats['new_newsletter_subscribers'],
                'new_with_gift':  stats['new_group_with_gift'],
            },
        }

        # Company name
        try:
            from django.db import connection
            company_name = getattr(connection.tenant, 'name', 'Кафе')
        except Exception:
            company_name = 'Кафе'

        context = {
            'title':             'Отчёт по системе лояльности',
            'company_name':      company_name,
            'report':            report_data,
            'charts_json':       json.dumps(charts_report),
            'branches':          branches,
            'active_branch_ids': branch_ids or [],
            'active_branches':   active_branches,
            'active_period':     active_period,
            'period_choices':    PERIOD_CHOICES,
            'period_qs':         _period_qs(active_period, start, end),
            'start':             start.isoformat(),
            'end':               end.isoformat(),
            'start_display':     start.strftime('%d.%m.%Y'),
            'end_display':       end.strftime('%d.%m.%Y'),
        }
        if request.GET.get('format') == 'pdf':
            response = render(request, 'analytics/loyalty_report_pdf.html', context)
            response['X-Frame-Options'] = 'SAMEORIGIN'
            return response
        return render(request, self.template_name, context)