"""
Одноразовая команда: пройти по всем тенантам, у каждого SenlerConfig взять
токен/группу, обойти ВСЕ существующие conv с vk_sender_id, и для каждого
скачать недостающие сообщения через cursor.

Восстанавливает пропущенные ответы менеджера (LU-35) — те, что не были пойманы
старым poll'ом с count=5 при наплыве рассылок.

Usage:
    docker exec web python manage.py backfill_vk_messages              # все тенанты
    docker exec web python manage.py backfill_vk_messages --schema asap_orel
    docker exec web python manage.py backfill_vk_messages --schema asap_orel --vk-sender 105394136
    docker exec web python manage.py backfill_vk_messages --dry-run    # ничего не пишем, только лог
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, schema_context

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Догнать пропущенные VK-сообщения по cursor для всех (или указанного) тенантов.'

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Только указанный schema_name (иначе все non-public).')
        parser.add_argument('--vk-sender', help='Только указанный vk_sender_id (иначе все conv с vk_sender_id).')
        parser.add_argument('--since-days', type=int, default=7,
            help='Игнорировать сообщения старше N дней. По умолчанию 7. Защита от рассылок за годы.')
        parser.add_argument('--dry-run', action='store_true', help='Не сохранять, только логировать что подтянули бы.')

    def handle(self, *args, **opts):
        target_schema = opts.get('schema')
        target_sender = opts.get('vk_sender')
        since_days = int(opts.get('since_days') or 7)
        dry_run = bool(opts.get('dry_run'))

        TenantModel = get_tenant_model()
        qs = TenantModel.objects.exclude(schema_name='public')
        if target_schema:
            qs = qs.filter(schema_name=target_schema)

        total_new = 0
        for tenant in qs:
            with schema_context(tenant.schema_name):
                count = self._process_tenant(tenant.schema_name, target_sender, since_days, dry_run)
            total_new += count
            self.stdout.write(f'[{tenant.schema_name}] new messages: {count}')

        self.stdout.write(self.style.SUCCESS(f'DONE. Total new messages: {total_new} (dry_run={dry_run})'))

    def _process_tenant(self, schema_name: str, target_sender: str | None, since_days: int, dry_run: bool) -> int:
        from apps.tenant.branch.models import TestimonialConversation
        from apps.tenant.branch.tasks import (
            _vk_call,
            _save_vk_message,
            FIRST_SEEN_BACKFILL,  # noqa: F401  — используем тот же модуль для cursor-логики
        )
        from apps.tenant.branch.api.services import (
            handle_vk_incoming_message,
            handle_vk_admin_reply_from_poll,
        )
        from apps.tenant.senler.models import SenlerConfig

        # Один токен на тенанта (вся сеть использует один vk_group_id/token).
        cfg = SenlerConfig.objects.filter(vk_community_token__gt='').first()
        if not cfg or not cfg.vk_community_token:
            self.stdout.write(f'  [{schema_name}] no SenlerConfig with token — skip')
            return 0
        token = cfg.vk_community_token
        group_id = cfg.vk_group_id

        # Кандидаты — все conv с известным vk_sender_id. Берём DISTINCT sender_id,
        # иначе будем дублировать для legacy + new conv одного гостя.
        senders_qs = (
            TestimonialConversation.objects
            .exclude(vk_sender_id='')
            .values_list('vk_sender_id', flat=True)
            .distinct()
        )
        if target_sender:
            senders_qs = senders_qs.filter(vk_sender_id=target_sender)
        senders = list(senders_qs)

        self.stdout.write(f'  [{schema_name}] {len(senders)} unique senders to backfill, since_days={since_days}')

        import time
        cutoff_ts = int(time.time()) - since_days * 86400
        new_count = 0
        for sender_id in senders:
            try:
                peer_id = int(sender_id)
            except (TypeError, ValueError):
                continue

            convs = list(
                TestimonialConversation.objects
                .filter(vk_sender_id=sender_id)
                .order_by('-last_message_at')
            )
            last_polled = max((int(c.last_polled_vk_msg_id or 0) for c in convs), default=0)

            fetched_max = last_polled
            pages = 0
            offset = 0
            # VK не разрешает rev=1 одновременно с start_message_id.
            # Используем offset-paging: rev=0 (newest first), 200/страница,
            # двигаем offset пока не наткнёмся на cursor или не упёрлись в лимит.
            while pages < 10:  # backfill — даём чуть больше страниц чем регулярный poll
                pages += 1
                params = {
                    'peer_id':  peer_id,
                    'group_id': group_id,
                    'count':    200,
                    'offset':   offset,
                    'rev':      0,
                    'mark_as_read': 0,
                }
                try:
                    hist = _vk_call('messages.getHistory', token, **params)
                except RuntimeError as e:
                    self.stderr.write(f'  [{schema_name}] sender {sender_id}: VK error: {e}')
                    break
                page_items = hist.get('items', [])
                if not page_items:
                    break

                page_max = max((int(m.get('id') or 0) for m in page_items), default=0)
                reached_cursor = False
                reached_cutoff = False
                for msg in page_items:
                    msg_id = int(msg.get('id') or 0)
                    if msg_id <= last_polled:
                        reached_cursor = True
                        break
                    msg_date = int(msg.get('date') or 0)
                    if msg_date and msg_date < cutoff_ts:
                        # Дошли до сообщений старше cutoff — больше не интересуют.
                        reached_cutoff = True
                        break
                    # Применяем тот же фильтр что и _save_vk_message: рассылки
                    # (out=1 без admin_author_id) пропускаем и в dry-run, чтобы
                    # логи не вводили в заблуждение.
                    if msg.get('out') == 1 and not msg.get('admin_author_id'):
                        continue
                    if dry_run:
                        text_preview = (msg.get('text') or '')[:80].replace('\n', ' ')
                        out = msg.get('out')
                        self.stdout.write(
                            f'  [{schema_name}] sender {sender_id} msg={msg_id} out={out} "{text_preview}"'
                        )
                        new_count += 1
                    else:
                        saved_pk = _save_vk_message(
                            group_id, msg,
                            handle_vk_incoming_message,
                            handle_vk_admin_reply_from_poll,
                        )
                        if saved_pk:
                            new_count += 1

                fetched_max = max(fetched_max, page_max)

                if reached_cursor or reached_cutoff:
                    break
                if len(page_items) < 200:
                    break
                offset += 200

            if not dry_run and fetched_max > last_polled:
                TestimonialConversation.objects.filter(
                    vk_sender_id=sender_id,
                ).update(last_polled_vk_msg_id=fetched_max)

        return new_count
