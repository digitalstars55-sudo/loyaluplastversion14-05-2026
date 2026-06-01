# -*- coding: utf-8 -*-
"""Дозаполнить reply_to-контекст («на что ответил гость») для свежих гостевых
VK-сообщений, у которых он пуст (LU-40+/LU-42). Только текст-контекст — НЕ
трогает is_replied/has_unread/sentiment, НЕ шлёт пуши. dry-run по умолчанию."""
import time
from datetime import datetime, timezone as dttz
from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

from apps.tenant.branch.tasks import (_vk_call, _last_outgoing_before,
                                      _context_from, CONTEXT_MAX_AGE_SEC)
from apps.tenant.branch.models import (TestimonialConversation as TC,
                                       TestimonialMessage as TM)
from apps.tenant.senler.models import SenlerConfig


class Command(BaseCommand):
    help = 'Backfill reply_to context for recent guest VK messages (LU-40+/LU-42).'

    def add_arguments(self, p):
        p.add_argument('--schemas', default='', help='comma list; empty=all')
        p.add_argument('--days', type=int, default=30)
        p.add_argument('--commit', action='store_true')
        p.add_argument('--clean-stale', action='store_true',
                       help='clear legacy context where gap > CONTEXT_MAX_AGE_SEC')

    def handle(self, *a, **o):
        from django.db.models import F
        schemas = [s.strip() for s in o['schemas'].split(',') if s.strip()]
        days, commit = o['days'], o['commit']
        clean_stale = o['clean_stale']
        since = timezone.now() - timezone.timedelta(days=days)
        TenantModel = get_tenant_model()
        tenants = TenantModel.objects.exclude(schema_name='public')
        if schemas:
            tenants = tenants.filter(schema_name__in=schemas)

        for t in tenants.order_by('schema_name'):
            with schema_context(t.schema_name):
                cfg = (SenlerConfig.objects.filter(is_active=True)
                       .exclude(vk_community_token='').first())
                if not cfg:
                    continue
                token, gid = cfg.vk_community_token, cfg.vk_group_id
                senders = list(
                    TM.objects.filter(source='VK_MESSAGE', reply_to_text='',
                                      created_at__gte=since)
                    .exclude(conversation__vk_sender_id='')
                    .values_list('conversation__vk_sender_id', flat=True).distinct())
                scanned = filled = 0
                for vsid in senders:
                    if not vsid:
                        continue
                    try:
                        time.sleep(0.34)
                        h = _vk_call('messages.getHistory', token, group_id=gid,
                                     user_id=int(vsid), count=50, rev=0)
                    except Exception:
                        continue
                    items = h.get('items', [])
                    for tm in TM.objects.filter(conversation__vk_sender_id=vsid,
                                                source='VK_MESSAGE', reply_to_text='',
                                                created_at__gte=since):
                        scanned += 1
                        try:
                            mid = int(tm.vk_message_id or 0)
                        except (TypeError, ValueError):
                            continue
                        if not mid:
                            continue
                        ref = int(tm.created_at.timestamp())
                        txt, dt = _context_from(_last_outgoing_before(items, mid, ref))
                        if txt:
                            filled += 1
                            if commit:
                                rdt = (datetime.fromtimestamp(int(dt), tz=dttz.utc)
                                       if dt else None)
                                TM.objects.filter(pk=tm.pk).update(
                                    reply_to_text=txt[:2000], reply_to_date=rdt)
                cleaned = 0
                if clean_stale:
                    stale = TM.objects.filter(
                        source='VK_MESSAGE', reply_to_date__isnull=False,
                        reply_to_date__lt=F('created_at') - timezone.timedelta(
                            seconds=CONTEXT_MAX_AGE_SEC))
                    cleaned = stale.count()
                    if commit and cleaned:
                        stale.update(reply_to_text='', reply_to_date=None)
                self.stdout.write('%-22s scanned=%-4d filled=%-4d cleaned=%-4d %s'
                                  % (t.schema_name, scanned, filled, cleaned,
                                     '(COMMIT)' if commit else '(dry-run)'))
        self.stdout.write('DONE')
