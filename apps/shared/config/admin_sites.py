import json as _json
import logging
import os
from datetime import date

from django.contrib.admin import AdminSite
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class PublicAdminSite(AdminSite):
    """
    Панель супер-администратора платформы.
    Доступна только пользователям с role=SUPERADMIN.
    Маршрут: /superadmin/  (public schema)
    """
    site_header = 'ЛоялUP Платформа'
    site_title = 'Супер Администратор'
    index_title = 'Управление платформой'
    index_template = 'admin/public_index.html'

    def has_permission(self, request):
        if not request.user.is_active or not request.user.is_authenticated:
            return False
        # is_superuser — единственный gate для уровня SUPERADMIN
        if request.user.is_superuser:
            return True
        # Только SUPERADMIN заходит в public_admin
        return getattr(request.user, 'role', None) == 'superadmin'

    def each_context(self, request):
        ctx = super().each_context(request)
        try:
            from django_tenants.utils import schema_context
            from apps.shared.clients.models import Company, Domain
            from apps.shared.clients.billing import payment_status

            qs = Company.objects.exclude(schema_name='public')
            if getattr(request.user, 'role', None) == 'network_admin':
                qs = qs.filter(pk__in=request.user.companies.values_list('pk', flat=True))
            companies = qs.prefetch_related('domains').order_by('name')
            total_branches = 0
            cards = []
            expiring_soon = 0  # счётчик: «осталось ≤ 10 дней» (включая просроченные)
            for c in companies:
                primary = next((d for d in c.domains.all() if d.is_primary), None)
                # get branch count from tenant schema
                branch_count = 0
                try:
                    with schema_context(c.schema_name):
                        from apps.tenant.branch.models import Branch
                        branch_count = Branch.objects.filter(is_active=True).count()
                except Exception:
                    pass
                total_branches += branch_count

                billing = payment_status(c.paid_until)
                if billing['needs_attention']:
                    expiring_soon += 1

                cards.append({
                    'id': c.pk,
                    'name': c.name,
                    'schema': c.schema_name,
                    'domain': primary.domain if primary else '—',
                    'is_active': c.is_active,
                    'paid_until': c.paid_until,
                    'branch_count': branch_count,
                    'admin_url': f'//{primary.domain}/admin/' if primary else '#',
                    'billing': billing,
                })

            active_count = sum(1 for c in cards if c['is_active'])
            domain_count = Domain.objects.count()

            ctx['infra_cards'] = cards
            ctx['infra_total'] = len(cards)
            ctx['infra_active'] = active_count
            ctx['infra_branches'] = total_branches
            ctx['infra_domains'] = domain_count
            ctx['infra_expiring_soon'] = expiring_soon
        except Exception:
            logger.exception('Public admin: failed to load infra context')
            ctx['infra_cards'] = []
            ctx['infra_total'] = 0
            ctx['infra_active'] = 0
            ctx['infra_branches'] = 0
            ctx['infra_domains'] = 0
            ctx['infra_expiring_soon'] = 0
        return ctx

    # ── Сводная статистика по всем клиентам ────────────────────────────────────

    def get_urls(self):
        from django.urls import path
        return [
            path('overview/', self.admin_view(self._overview_view), name='cross_overview'),
            path('overview/reviews/', self.admin_view(self._overview_reviews_view), name='cross_overview_reviews'),
            path('audit/', self.admin_view(self._audit_view), name='cross_audit'),
            path('discovery/', self.admin_view(self._discovery_view), name='cross_discovery'),
        ] + super().get_urls()

    def _audit_view(self, request):
        """
        /superadmin/audit/ — журнал действий всех участников системы (кто/роль/
        ник, какой клиент, что сделал, эндпоинт, время, IP). Фильтры + пагинация.
        Только суперадмин (через self.admin_view → has_permission).
        """
        from datetime import date as _date
        from django.core.paginator import Paginator
        from django.db.models import Q
        from django.template.response import TemplateResponse
        from apps.shared.audit.models import AuditEvent

        qs = AuditEvent.objects.select_related('actor').all()

        f_actor  = (request.GET.get('actor') or '').strip()
        f_tenant = (request.GET.get('tenant') or '').strip()
        f_action = (request.GET.get('action') or '').strip()
        f_q      = (request.GET.get('q') or '').strip()
        f_start  = (request.GET.get('start') or '').strip()
        f_end    = (request.GET.get('end') or '').strip()

        if f_actor:
            qs = qs.filter(actor_username=f_actor)
        if f_tenant:
            qs = qs.filter(tenant_schema=f_tenant)
        if f_action:
            qs = qs.filter(action=f_action)
        if f_q:
            qs = qs.filter(
                Q(path__icontains=f_q) | Q(target__icontains=f_q)
                | Q(ip__icontains=f_q) | Q(actor_username__icontains=f_q)
            )
        for raw, lookup in ((f_start, 'created_at__date__gte'), (f_end, 'created_at__date__lte')):
            if raw:
                try:
                    qs = qs.filter(**{lookup: _date.fromisoformat(raw)})
                except ValueError:
                    pass

        total = qs.count()
        paginator = Paginator(qs, 60)
        page_obj = paginator.get_page(request.GET.get('page'))

        # Опции фильтров (distinct по журналу — на нашем масштабе дёшево).
        actor_options = list(
            AuditEvent.objects.exclude(actor_username='')
            .values_list('actor_username', flat=True).distinct().order_by('actor_username')
        )
        tenant_options = list(
            AuditEvent.objects.exclude(tenant_schema='')
            .values_list('tenant_schema', 'tenant_name').distinct().order_by('tenant_schema')
        )

        # query-string без page (для ссылок пагинации).
        params = request.GET.copy()
        params.pop('page', None)
        base_qs = params.urlencode()

        ctx = self.each_context(request)
        ctx.update({
            'title': 'Журнал действий',
            'page_obj': page_obj,
            'total': total,
            'action_choices': AuditEvent.Action.choices,
            'actor_options': actor_options,
            'tenant_options': tenant_options,
            'f_actor': f_actor, 'f_tenant': f_tenant, 'f_action': f_action,
            'f_q': f_q, 'f_start': f_start, 'f_end': f_end,
            'base_qs': base_qs,
        })
        return TemplateResponse(request, 'admin/audit/audit.html', ctx)

    def _discovery_view(self, request):
        """
        /superadmin/discovery/ — воронка сетевого входа из каталога VK
        (open → play → «Забрать» → выбрал город → активировал на кассе) +
        разбивка по городам. Кросс-тенантно из публичной схемы (без обхода схем).
        Только суперадмин (через self.admin_view → has_permission).
        """
        from django.db.models import Count
        from django.template.response import TemplateResponse
        from apps.shared.clients.cross_stats import (
            parse_overview_period, OVERVIEW_PERIODS, overview_period_qs,
        )
        from apps.shared.discovery.models import (
            DiscoveryEvent, DiscoveryStage, DiscoveryClaim,
        )

        start, end, active_period = parse_overview_period(request)

        def _ev(stage):
            return DiscoveryEvent.objects.filter(
                stage=stage, created_at__date__gte=start, created_at__date__lte=end,
            ).count()

        opens   = _ev(DiscoveryStage.OPEN)
        plays   = _ev(DiscoveryStage.PLAY)
        claim_opens = _ev(DiscoveryStage.CLAIM_OPEN)
        uniq_open = (
            DiscoveryEvent.objects
            .filter(stage=DiscoveryStage.OPEN, created_at__date__gte=start, created_at__date__lte=end)
            .values('vk_id').distinct().count()
        )
        chosen = DiscoveryClaim.objects.filter(
            created_at__date__gte=start, created_at__date__lte=end,
        ).count()
        redeemed = DiscoveryClaim.objects.filter(
            redeemed_at__date__gte=start, redeemed_at__date__lte=end,
        ).count()

        def _pct(a, b):
            return round(a * 100 / b) if b else 0

        funnel = [
            {'label': 'Заходов из каталога', 'value': opens, 'hint': f'{uniq_open} уник.', 'cls': 'c-blue', 'ic': 'ic-blue', 'emoji': '👀'},
            {'label': 'Сыграли', 'value': plays, 'hint': f'{_pct(plays, opens)}% от заходов', 'cls': 'c-violet', 'ic': 'ic-violet', 'emoji': '🎰'},
            {'label': 'Нажали «Забрать»', 'value': claim_opens, 'hint': f'{_pct(claim_opens, plays)}% от сыгравших', 'cls': 'c-amber', 'ic': 'ic-amber', 'emoji': '🎁'},
            {'label': 'Выбрали город', 'value': chosen, 'hint': f'{_pct(chosen, claim_opens)}% от нажавших', 'cls': 'c-orange', 'ic': 'ic-orange', 'emoji': '📍'},
            {'label': 'Активировали на кассе', 'value': redeemed, 'hint': f'{_pct(redeemed, chosen)}% дошли в офлайн', 'cls': 'c-green', 'ic': 'ic-green', 'emoji': '✅'},
        ]

        by_city = list(
            DiscoveryClaim.objects
            .filter(created_at__date__gte=start, created_at__date__lte=end)
            .values('city').annotate(chosen=Count('id')).order_by('-chosen')
        )
        redeemed_map = dict(
            DiscoveryClaim.objects
            .filter(redeemed_at__date__gte=start, redeemed_at__date__lte=end)
            .values_list('city').annotate(c=Count('id'))
        )
        cities = []
        for row in by_city:
            city = row['city'] or '—'
            ch = row['chosen']
            rd = redeemed_map.get(row['city'], 0)
            cities.append({'city': city, 'chosen': ch, 'redeemed': rd, 'conv': _pct(rd, ch)})

        ctx = self.each_context(request)
        ctx.update({
            'title': 'Сетевой вход (VK-каталог)',
            'funnel': funnel,
            'cities': cities,
            'conv_online_offline': _pct(redeemed, chosen),
            'period_choices': OVERVIEW_PERIODS,
            'active_period': active_period,
            'period_qs': overview_period_qs(active_period, start, end),
            'start': start,
            'end': end,
        })
        return TemplateResponse(request, 'admin/discovery/funnel.html', ctx)

    def _overview_view(self, request):
        """
        /admin/overview/ — сводная статистика сразу по всем подключённым клиентам
        за выбранный период (вчера/сегодня/7д/30д/диапазон). Только суперадмин
        (через self.admin_view → has_permission).
        """
        from django.template.response import TemplateResponse
        from apps.shared.clients.cross_stats import (
            get_cross_tenant_overview, parse_overview_period, OVERVIEW_PERIODS,
            overview_period_qs,
        )

        start, end, active_period = parse_overview_period(request)
        try:
            data = get_cross_tenant_overview(start, end)
        except Exception:
            logger.exception('Public admin: cross-tenant overview failed')
            data = {'rows': [], 'totals': {}, 'client_count': 0}

        ctx = self.each_context(request)
        ctx.update({
            'title': 'Сводная статистика',
            'rows': data['rows'],
            'totals': data['totals'],
            'feed': data.get('feed', []),
            'client_count': data['client_count'],
            'period_choices': OVERVIEW_PERIODS,
            'active_period': active_period,
            'period_qs': overview_period_qs(active_period, start, end),
            'start': start,
            'end': end,
        })
        return TemplateResponse(request, 'admin/clients/overview.html', ctx)

    def _overview_reviews_view(self, request):
        """
        /admin/overview/reviews/ — все отзывы со ВСЕХ клиентов за период,
        с фильтром по типу (Все/Позитивные/Нейтральные/Негативные) и пагинацией.
        """
        from django.core.paginator import Paginator
        from django.template.response import TemplateResponse
        from apps.shared.clients.cross_stats import (
            get_cross_tenant_reviews, parse_overview_period,
            OVERVIEW_PERIODS, SENTIMENT_FILTERS, overview_period_qs,
        )

        start, end, active_period = parse_overview_period(request)
        sentiment = request.GET.get('sentiment', 'all')
        if sentiment not in dict(SENTIMENT_FILTERS):
            sentiment = 'all'
        try:
            reviews = get_cross_tenant_reviews(start, end, sentiment)
        except Exception:
            logger.exception('Public admin: cross-tenant reviews failed')
            reviews = []

        paginator = Paginator(reviews, 30)
        page_obj = paginator.get_page(request.GET.get('page'))

        ctx = self.each_context(request)
        ctx.update({
            'title': 'Все отзывы',
            'page_obj': page_obj,
            'total': len(reviews),
            'period_choices': OVERVIEW_PERIODS,
            'active_period': active_period,
            'sentiment': sentiment,
            'sentiment_filters': SENTIMENT_FILTERS,
            'period_qs': overview_period_qs(active_period, start, end),
            'start': start,
            'end': end,
        })
        return TemplateResponse(request, 'admin/clients/overview_reviews.html', ctx)


class TenantAdminSite(AdminSite):
    """
    Панель администратора сети/точки ресторана.
    Доступна NETWORK_ADMIN и CLIENT текущего тенанта.
    Маршрут: /admin/  (tenant schema)
    """
    site_header = 'ЛоялUP'
    site_title = 'Панель управления'
    index_title = 'Управление рестораном'
    index_template = 'admin/tenant_index.html'

    def has_permission(self, request):
        if not request.user.is_active or not request.user.is_authenticated:
            return False
        # is_superuser — единственный gate для уровня SUPERADMIN
        if request.user.is_superuser:
            return True
        role = getattr(request.user, 'role', None)
        if role not in ('superadmin', 'network_admin', 'client'):
            return False
        # superadmin заходит на любой тенант без проверки компаний
        if role == 'superadmin':
            return True
        # Проверяем, есть ли текущий тенант в списке компаний пользователя
        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return False
        return request.user.companies.filter(pk=tenant.pk).exists()

    def get_app_list(self, request, app_label=None):
        # Клиенты не видят никакие модели в admin
        if getattr(request.user, 'role', None) == 'client':
            return []
        return super().get_app_list(request, app_label=app_label)

    def each_context(self, request):
        ctx = super().each_context(request)
        ctx['user_is_client'] = getattr(request.user, 'role', None) == 'client'

        # Billing — статус оплаты для текущего тенанта (компании пользователя).
        # Берётся из request.tenant, который проставляет django-tenants middleware.
        try:
            from apps.shared.clients.billing import payment_status
            tenant = getattr(request, 'tenant', None)
            if tenant is not None and getattr(tenant, 'paid_until', None):
                ctx['tenant_billing'] = payment_status(tenant.paid_until)
                ctx['tenant_name']    = tenant.name
        except Exception:
            logger.exception('Tenant admin: failed to compute billing status')

        # Тонкое разграничение: доступные точки (branch_access) + разделы (feature_access).
        from apps.shared.users.access import (
            user_allowed_branches, user_can_feature, current_schema_name,
        )
        schema = current_schema_name()
        allowed_branches = user_allowed_branches(request.user, schema)  # None=все

        try:
            from apps.tenant.branch.models import Branch, DailyCode, current_code_date
            today = current_code_date()  # кодовые сутки начинаются в 03:00 MSK
            branches = Branch.objects.filter(is_active=True).order_by('name')
            if allowed_branches is not None:
                branches = branches.filter(pk__in=allowed_branches)
            codes_qs = DailyCode.objects.filter(valid_date=today).select_related('branch')

            codes_map = {}
            for dc in codes_qs:
                codes_map.setdefault(dc.branch_id, {})[dc.purpose] = dc.code

            rows = []
            for br in branches:
                bc = codes_map.get(br.pk, {})
                rows.append({
                    'name': br.name,
                    'game': bc.get('game', '—'),
                    'quest': bc.get('quest', '—'),
                    'birthday': bc.get('birthday', '—'),
                })
            ctx['daily_code_rows'] = rows
            ctx['daily_code_date'] = today
        except Exception:
            ctx['daily_code_rows'] = []
            ctx['daily_code_date'] = None

        # Флаги разделов для шаблона (feature_access). SU/без ограничений → всё True.
        ctx['can_daily_codes']    = user_can_feature(request.user, 'daily_codes')
        ctx['can_general_stats']  = user_can_feature(request.user, 'general_stats')
        ctx['can_analytics']      = user_can_feature(request.user, 'analytics')
        ctx['can_contact_points'] = user_can_feature(request.user, 'contact_points')
        ctx['show_analytics'] = (
            ctx['can_general_stats'] or ctx['can_analytics'] or ctx['can_contact_points']
        )
        return ctx

    # ── Custom admin URLs ──────────────────────────────────────────────────────

    def get_urls(self):
        from django.urls import path
        return [
            path('ai/generate/', self.admin_view(self._ai_generate_view), name='ai_generate'),
        ] + super().get_urls()

    def _ai_generate_view(self, request):
        """
        POST /admin/ai/generate/
        Body: {
          "draft": "...",            # current textarea value (may be empty)
          "type": "reply|broadcast", # context
          "conversation_id": 123,    # required for type=reply
          "broadcast_type": "birthday_7d|birthday_1d|birthday|after_game_3h"
        }
        Returns: {"text": "..."}  or  {"error": "..."}
        """
        from django.conf import settings

        if request.method != 'POST':
            return JsonResponse({'error': 'POST required'}, status=405)

        try:
            body = _json.loads(request.body)
        except Exception:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        draft          = body.get('draft', '')
        context_type   = body.get('type', 'broadcast')
        conv_id        = body.get('conversation_id')
        broadcast_type = body.get('broadcast_type', '')

        # ── Load KnowledgeBase instructions ───────────────────────────────────
        instructions = ''
        try:
            from apps.tenant.analytics.models import KnowledgeBaseDocument
            docs = KnowledgeBaseDocument.objects.filter(is_active=True).exclude(extracted_text='')
            instructions = '\n\n'.join(
                f'=== {doc.title} ===\n{doc.extracted_text}' for doc in docs
            )
        except Exception as e:
            logger.warning('AI generate: failed to load KnowledgeBase: %s', e)

        # ── Load guest messages for reply context ─────────────────────────────
        guest_context = ''
        if context_type == 'reply' and conv_id:
            try:
                from apps.tenant.branch.models import TestimonialConversation, TestimonialMessage
                conv = TestimonialConversation.objects.get(pk=conv_id)
                msgs = (
                    conv.messages
                    .exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
                    .order_by('created_at')
                    .values_list('text', flat=True)
                )
                guest_context = '\n---\n'.join(m for m in msgs if m.strip())
            except Exception as e:
                logger.warning('AI generate: failed to load conversation %s: %s', conv_id, e)

        # ── Build prompts ──────────────────────────────────────────────────────
        if context_type == 'reply':
            system_prompt = (
                'Ты профессиональный менеджер ресторана. Твоя задача — написать ответ на отзыв гостя.\n'
                f'СПРАВКА О ЗАВЕДЕНИИ ИЗ БАЗЫ ЗНАНИЙ (тон общения и факты; '
                f'НЕ копируй отсюда готовые ответы как шаблон):\n{instructions}\n\n'
                'Проанализируй отзыв и напиши идеальный ответ. Если есть черновик ответа — '
                'это указание администратора, обязательно следуй ему и улучши, сохранив смысл.\n'
                'По умолчанию ответ короткий (3-4 предложения). Если в черновике администратор явно '
                'просит написать подробнее — выполни просьбу, до 4000 символов.\n'
                'Ответ должен быть готовым к отправке (без кавычек и вступительных слов «Вот ответ…»).'
            )
            user_msg = f'Сообщения гостя:\n{guest_context}' if guest_context else 'Гость написал сообщение.'
            if draft:
                user_msg += f'\n\nЧерновик ответа: {draft}'
        else:
            type_labels = {
                'birthday_7d':   'за 7 дней до дня рождения',
                'birthday_1d':   'за 1 день до дня рождения',
                'birthday':      'в день рождения',
                'after_game_3h': 'через 3 часа после игры в мини-игру',
            }
            hint = type_labels.get(broadcast_type, '')
            kb_block = (
                'СПРАВКА О ЗАВЕДЕНИИ ИЗ БАЗЫ ЗНАНИЙ (используй для тона, фактов '
                'и действующих акций; НЕ копируй отсюда готовые тексты рассылок):\n'
                f'{instructions}'
                if instructions else
                'База знаний заведения пуста.'
            )
            system_prompt = (
                'Ты профессиональный маркетолог ресторана. Твоя задача — написать рассылочное сообщение для гостей.\n'
                f'{kb_block}\n\n'
                'Если пользователь дал конкретное задание/черновик — это ГЛАВНОЕ: '
                'пиши строго про то, что он просит (повод, адрес, скидку, дату). '
                'База знаний — только фон, не подменяй ею задание пользователя.\n'
                'Длина: до 2000 символов. Если пользователь явно просит написать '
                'длиннее — до 4000 символов (лимит VK).\n'
                'Напиши готовое к отправке сообщение (без кавычек и вступительных слов).'
            )
            if draft:
                user_msg = (
                    f'ЗАДАНИЕ ОТ ПОЛЬЗОВАТЕЛЯ — выполни именно его:\n{draft}\n\n'
                    f'Напиши на основе этого задания готовый текст рассылки. '
                    f'Не заменяй его общим приветствием.'
                )
            else:
                user_msg = f'Напиши сообщение для рассылки{f" ({hint})" if hint else ""}.'

        # ── Call Claude Haiku via proxy ────────────────────────────────────────
        api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
        if not api_key:
            return JsonResponse({'error': 'ANTHROPIC_API_KEY не настроен'}, status=500)

        try:
            import anthropic

            proxy_url = os.getenv('AI_PROXY_URL', '')
            if proxy_url:
                client = anthropic.Anthropic(
                    api_key=api_key,
                    base_url=proxy_url,
                )
            else:
                client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=2048,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_msg}],
            )
            return JsonResponse({'text': message.content[0].text.strip()})

        except Exception as e:
            logger.exception('AI generate failed: %s', e)
            return JsonResponse({'error': str(e)}, status=500)


public_admin = PublicAdminSite(name='public_admin')
tenant_admin = TenantAdminSite(name='tenant_admin')
