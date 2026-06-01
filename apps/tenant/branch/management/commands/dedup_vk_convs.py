"""
Merge duplicate TestimonialConversations (same branch + vk_sender_id) — LU-42.

A race between poll and the VK Callback during the reconcile flood created a 2nd
conversation for the same (branch, vk_sender_id). get_or_create's internal .get()
then raised MultipleObjectsReturned and crashed the whole poll_all_vk_messages_task.

Per VK tenant, for each (branch_id, vk_sender_id) group with >1 conversation:
  * primary = the one with the most messages (oldest created_at breaks ties);
  * move the other convs' messages onto primary (skipping vk_message_id dupes);
  * carry over vk_guest / client link if primary lacks it;
  * delete the now-empty extras;
  * recompute primary's is_replied / has_unread / last_message_at.

Only merges SAME-branch dupes — never merges a legacy branch=X (APP) thread with a
branch=None (VK) thread (those stay split; the UI unifies them via the sources array).

Dry-run by default. Pass --commit to write. Idempotent.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

GUEST_SOURCES = ('VK_MESSAGE', 'APP')


class Command(BaseCommand):
    help = 'Merge duplicate TestimonialConversations (same branch+vk_sender_id) — LU-42.'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true')
        parser.add_argument('--schemas', default='')

    def handle(self, *args, **opts):
        commit = opts['commit']
        from apps.tenant.senler.models import SenlerConfig
        TenantModel = get_tenant_model()
        if opts['schemas']:
            schemas = [s.strip() for s in opts['schemas'].split(',') if s.strip()]
        else:
            schemas = []
            for t in TenantModel.objects.exclude(schema_name='public'):
                with schema_context(t.schema_name):
                    if SenlerConfig.objects.exists():
                        schemas.append(t.schema_name)

        mode = 'COMMIT' if commit else 'DRY-RUN'
        tg = td = tm = 0
        for schema in schemas:
            with schema_context(schema):
                g, d, m = self._dedup(schema, commit)
                tg += g
                td += d
                tm += m
        self.stdout.write('[%s] TOTAL groups=%s convs_deleted=%s msgs_moved=%s'
                          % (mode, tg, td, tm))

    def _dedup(self, schema, commit):
        from apps.tenant.branch.models import (
            TestimonialConversation as TC, TestimonialMessage as TM,
        )
        groups = (TC.objects.exclude(vk_sender_id__isnull=True).exclude(vk_sender_id='')
                  .values('branch_id', 'vk_sender_id')
                  .annotate(c=Count('id')).filter(c__gt=1))
        ng = nd = nm = 0
        for grp in groups:
            convs = list(TC.objects.filter(branch_id=grp['branch_id'],
                                           vk_sender_id=grp['vk_sender_id']))
            if len(convs) < 2:
                continue
            ng += 1
            convs.sort(key=lambda c: (c.messages.count(), -c.created_at.timestamp()),
                       reverse=True)
            primary, others = convs[0], convs[1:]
            existing = set(TM.objects.filter(conversation=primary)
                           .exclude(vk_message_id='')
                           .values_list('vk_message_id', flat=True))
            for o in others:
                for msg in list(o.messages.all()):
                    if msg.vk_message_id and msg.vk_message_id in existing:
                        if commit:
                            msg.delete()
                    else:
                        if msg.vk_message_id:
                            existing.add(msg.vk_message_id)
                        if commit:
                            TM.objects.filter(pk=msg.pk).update(conversation=primary)
                        nm += 1
                if commit:
                    upd = []
                    if not primary.vk_guest_id and o.vk_guest_id:
                        primary.vk_guest_id = o.vk_guest_id
                        upd.append('vk_guest')
                    if not primary.client_id and o.client_id:
                        primary.client_id = o.client_id
                        upd.append('client')
                    if upd:
                        primary.save(update_fields=upd)
                    o.delete()
                nd += 1
            if commit:
                self._recompute(primary)

        self.stdout.write('  %s: groups=%s convs_deleted=%s msgs_moved=%s'
                          % (schema, ng, nd, nm))
        return ng, nd, nm

    def _recompute(self, conv):
        from apps.tenant.branch.models import TestimonialMessage as TM
        msgs = list(conv.messages.all())
        guest = [m for m in msgs if m.source in GUEST_SOURCES]
        admin = [m for m in msgs if m.source == TM.Source.ADMIN_REPLY]

        def vi(m):
            try:
                return int(m.vk_message_id)
            except (TypeError, ValueError):
                return 0
        ng = max((vi(m) for m in guest), default=0)
        na = max((vi(m) for m in admin), default=0)
        now = timezone.now()
        newest_guest = max((m.created_at for m in guest), default=None)
        is_replied = bool(admin) and na >= ng
        has_unread = (not is_replied) and newest_guest is not None \
            and newest_guest >= now - timedelta(days=30)
        dates = [m.created_at for m in msgs]
        last_at = max(dates) if dates else conv.created_at
        conv.is_replied = is_replied
        conv.has_unread = has_unread
        conv.last_message_at = last_at
        conv.save(update_fields=['is_replied', 'has_unread', 'last_message_at', 'updated_at'])
