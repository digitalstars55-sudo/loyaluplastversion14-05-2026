"""
Analytics API views — request/response only, no business logic.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
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
        # Если есть период — фильтруем список тем же набором ClientBranch,
        # что попадает в матрицу (чтобы count и длина списка были согласованы).
        if r_score is not None and f_score is not None:
            active_cb_ids = None
            if start_date and end_date:
                active_cb_ids = services._get_active_client_branch_ids(
                    branch_ids, start_date, end_date, mode,
                )
            guests = services.get_rf_segment_guests(
                branch_ids, r_score, f_score, mode=mode,
                client_branch_ids=active_cb_ids,
            )
            matrix = services.get_rf_matrix(
                branch_ids, mode=mode, client_branch_ids=active_cb_ids,
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
        from apps.tenant.analytics.models import RFSegment
        from apps.tenant.branch.models import Branch
        from apps.tenant.senler.models import Broadcast, AudienceType
        from apps.tenant.senler.services import create_send, run_broadcast

        segment_id   = request.data.get('segment_id')
        message_text = (request.data.get('message_text') or '').strip()
        mode         = request.data.get('mode', 'restaurant')
        branch_ids   = request.data.get('branch_ids', '')
        image_file   = request.FILES.get('image')

        if not message_text:
            return Response({'error': 'Текст рассылки не может быть пустым'}, status=status.HTTP_400_BAD_REQUEST)
        if len(message_text) > 4096:
            return Response({'error': 'Текст превышает лимит VK (4096 символов)'}, status=status.HTTP_400_BAD_REQUEST)

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

        results = []
        for branch in branches:
            broadcast = Broadcast.objects.create(
                branch=branch,
                name=broadcast_label,
                message_text=message_text,
                audience_type=AudienceType.ALL,
                image=image_file if image_file else None,
            )
            if segment is not None:
                broadcast.rf_segments.set([segment])
            # Без сегмента rf_segments остаются пустыми → resolve_recipients
            # вернёт всех активных оцифрованных гостей этой точки.

            triggered_by = getattr(request.user, 'username', 'api')
            send = create_send(broadcast, triggered_by=triggered_by, trigger_type='manual')

            try:
                run_broadcast(send)
                send.refresh_from_db()
                results.append({
                    'branch':    branch.name,
                    'branch_id': branch.pk,
                    'status':    send.status,
                    'sent':      send.sent_count,
                    'failed':    send.failed_count,
                    'skipped':   send.skipped_count,
                    'total':     send.recipients_count,
                    'error':     send.error_message or '',
                })
            except Exception as e:
                results.append({
                    'branch':    branch.name,
                    'branch_id': branch.pk,
                    'status':    'failed',
                    'error':     str(e),
                })

        total_sent = sum(r.get('sent', 0) for r in results)
        return Response({
            'ok':         True,
            'segment':    f'{segment.emoji} {segment.name}' if segment else 'Все оцифрованные гости',
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
            '- Пиши на русском, дружелюбно, коротко (2-4 предложения).\n'
            '- Не используй markdown, HTML, заглавные буквы целыми словами.\n'
            '- Не используй скобки и эмодзи чаще 1-2 раз.\n'
            '- Текст должен быть готов к отправке — без плейсхолдеров.\n'
            '- Лимит: 300 символов.\n'
            '- Верни ТОЛЬКО текст рассылки, без пояснений.'
        )

        if segment is not None:
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
            'Ты — менеджер системы лояльности ресторана/кафе. Пишешь комментарий для отчёта.\n'
            'Правила:\n'
            '- Пиши на русском, профессионально, коротко (2-3 предложения).\n'
            '- Анализируй данные и делай конкретные выводы.\n'
            '- Не используй markdown, HTML.\n'
            '- Для секций 10 и 11 пиши 3 пункта через символ новой строки, каждый начиная с «•».\n'
            '- Верни ТОЛЬКО текст комментария, без пояснений.'
        )

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
