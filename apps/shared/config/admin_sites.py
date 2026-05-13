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
    site_header = 'LevOne Platform'
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


class TenantAdminSite(AdminSite):
    """
    Панель администратора сети/точки ресторана.
    Доступна NETWORK_ADMIN и CLIENT текущего тенанта.
    Маршрут: /admin/  (tenant schema)
    """
    site_header = 'LevOne'
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

        try:
            from apps.tenant.branch.models import Branch, DailyCode
            today = date.today()
            branches = Branch.objects.filter(is_active=True).order_by('name')
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
                f'TONE OF VOICE / ИНСТРУКЦИИ:\n{instructions}\n\n'
                'Проанализируй отзыв и напиши идеальный ответ. Если есть черновик ответа, улучши его, сохраняя смысл.\n'
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
            system_prompt = (
                'Ты профессиональный маркетолог ресторана. Твоя задача — написать рассылочное сообщение для гостей.\n'
                f'TONE OF VOICE / ИНСТРУКЦИИ:\n{instructions}\n\n'
                'Напиши готовое к отправке сообщение (без кавычек и вступительных слов).'
            )
            user_msg = f'Напиши сообщение для рассылки{f" ({hint})" if hint else ""}.'
            if draft:
                user_msg += f' Черновик: {draft}'

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
                max_tokens=512,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_msg}],
            )
            return JsonResponse({'text': message.content[0].text.strip()})

        except Exception as e:
            logger.exception('AI generate failed: %s', e)
            return JsonResponse({'error': str(e)}, status=500)


public_admin = PublicAdminSite(name='public_admin')
tenant_admin = TenantAdminSite(name='tenant_admin')
