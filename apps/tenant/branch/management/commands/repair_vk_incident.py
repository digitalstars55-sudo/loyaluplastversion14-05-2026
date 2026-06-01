"""
One-off repair for the 2026-06-01 reconcile incident (LU-42).

Reconcile/backfill dug up the ENTIRE VK history into TestimonialConversations:
  * ~600 brand-new conversations per tenant created from years-old guest messages
    (created today, newest guest message months/years ago) — pure clutter.
  * sentiment defaults to WAITING -> reclassify_waiting_reviews_task churned the
    whole backlog, re-flagging has_unread, bumping last_message_at=now, sending
    review_new / draft_ready pushes for ancient messages.
  * promo broadcasts that slipped past the admin_author_id filter got saved as
    ADMIN_REPLY, floating old convs to the top and corrupting is_replied.

Per VK-enabled tenant this command:
  1. DELETES conversations created TODAY whose newest GUEST message is older than
     --delete-age-days (default 7) or that have no real (non-broadcast) message at
     all (dug-up history / broadcast-only phantoms). Recent real reviews are kept.
  2. STRIPS broadcast messages wrongly saved as ADMIN_REPLY (matched against
     BroadcastRecipient.vk_message_id, plus a long-text/many-convs heuristic for
     broadcasts sent outside our system).
  3. REPAIRS every surviving conversation: recomputes is_replied / has_unread /
     last_message_at from real (non-broadcast) messages, clears stale AI drafts on
     answered/historical threads, and moves historical WAITING -> NEUTRAL so the
     reclassify loop stops touching them.

Dry-run by default. Pass --commit to write. Safe to re-run (idempotent).
"""
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

GUEST_SOURCES = ('VK_MESSAGE', 'APP')
HEUR_MIN_CONVS = 10   # same admin-reply text in >=N convs ...
HEUR_MIN_LEN = 60     # ... and longer than this -> treat as broadcast


class Command(BaseCommand):
    help = 'Repair the 2026-06-01 VK reconcile incident (LU-42).'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true',
                            help='Persist changes (default: dry-run).')
        parser.add_argument('--schemas', default='',
                            help='Comma-separated schemas (default: all VK tenants).')
        parser.add_argument('--delete-age-days', type=int, default=7,
                            help='Created-today convs whose newest guest msg is older '
                                 'than this many days get deleted.')
        parser.add_argument('--no-heuristic', action='store_true',
                            help='Strip ONLY broadcasts proven via BroadcastRecipient; '
                                 'do not use the long-text/many-convs heuristic.')
        parser.add_argument('--no-delete', action='store_true',
                            help='Never delete dug-up historical convs (poll just '
                                 're-imports them) — only neutralize flags. Empty '
                                 'broadcast-only phantoms are still removed.')

    def handle(self, *args, **opts):
        commit = opts['commit']
        age_days = opts['delete_age_days']
        self._no_heuristic = opts['no_heuristic']
        self._no_delete = opts['no_delete']
        now = timezone.now()
        today = now.date()
        hist_cutoff = now - timedelta(hours=6)
        del_cutoff = now - timedelta(days=age_days)

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
        self.stdout.write('[%s] schemas=%s delete_age_days=%s' % (mode, schemas, age_days))

        totals = defaultdict(int)
        for schema in schemas:
            with schema_context(schema):
                self._process(schema, now, today, hist_cutoff, del_cutoff, commit, totals)
        self.stdout.write('[%s] TOTALS %s' % (mode, dict(totals)))

    def _process(self, schema, now, today, hist_cutoff, del_cutoff, commit, totals):
        from apps.tenant.branch.models import (
            TestimonialConversation as TC,
            TestimonialMessage as TM,
        )

        # Known broadcasts sent via our run_broadcast (reliable: vk_message_id).
        try:
            from apps.tenant.senler.models import BroadcastRecipient
            # vk_message_id is a numeric field — DON'T compare to '' (ValueError).
            known_bc = set(
                str(v) for v in BroadcastRecipient.objects
                .exclude(vk_message_id__isnull=True)
                .values_list('vk_message_id', flat=True)
                if v
            )
        except Exception:
            known_bc = set()

        # Heuristic: long admin-reply text repeated across many convs = broadcast
        # (covers broadcasts sent outside our system, e.g. manual VK community send).
        text_convs = defaultdict(set)
        for m in (TM.objects.filter(source=TM.Source.ADMIN_REPLY)
                  .exclude(text='').values('text', 'conversation_id')):
            t = (m['text'] or '').strip()
            if len(t) >= HEUR_MIN_LEN:
                text_convs[t].add(m['conversation_id'])
        heur_bc = {t for t, cs in text_convs.items() if len(cs) >= HEUR_MIN_CONVS}

        def bc_kind(m):
            """Return 'reliable' | 'heuristic' | None."""
            if m.source != TM.Source.ADMIN_REPLY:
                return None
            if m.vk_message_id and str(m.vk_message_id) in known_bc:
                return 'reliable'
            if self._no_heuristic:
                return None
            t = (m.text or '').strip()
            if len(t) >= HEUR_MIN_LEN and t in heur_bc:
                return 'heuristic'
            return None

        def vkint(m):
            try:
                return int(m.vk_message_id)
            except (TypeError, ValueError):
                return 0

        del_n = repaired = bc_stripped = drafts_cleared = waiting_neutral = 0
        bc_reliable = bc_heur = 0
        buckets = defaultdict(int)
        bc_ids = []

        for conv in TC.objects.prefetch_related('messages').all():
            msgs = list(conv.messages.all())
            guest = [m for m in msgs if m.source in GUEST_SOURCES]
            bc = [m for m in msgs if bc_kind(m)]
            admin_real = [m for m in msgs
                          if m.source == TM.Source.ADMIN_REPLY and m not in bc]
            newest_guest = max((m.created_at for m in guest), default=None)

            created_today = conv.created_at.date() == today
            no_recent_guest = newest_guest is None or newest_guest < del_cutoff
            will_be_empty = not guest and not admin_real

            # DELETE: broadcast-only phantom (always), or — unless --no-delete —
            # a dug-up historical conv created today. With --no-delete we keep &
            # neutralize instead, because poll just re-imports deleted convs.
            should_delete = will_be_empty or (
                not self._no_delete and created_today and no_recent_guest)
            if should_delete:
                del_n += 1
                if will_be_empty and not (created_today and no_recent_guest):
                    buckets['empty/bc-only'] += 1
                elif newest_guest is None:
                    buckets['no-guest'] += 1
                else:
                    d = (now - newest_guest).days
                    buckets['>365' if d > 365 else '>90' if d > 90
                            else '>30' if d > 30 else '7-30'] += 1
                if commit:
                    conv.delete()  # cascades messages
                continue

            # REPAIR surviving conv (collect its broadcast msg ids for stripping).
            for m in bc:
                bc_ids.append(m.id)
                if bc_kind(m) == 'reliable':
                    bc_reliable += 1
                else:
                    bc_heur += 1
            ng_id = max((vkint(m) for m in guest), default=0)
            na_id = max((vkint(m) for m in admin_real), default=0)
            is_replied = bool(admin_real) and na_id >= ng_id
            has_unread = (not is_replied) and newest_guest is not None \
                and newest_guest >= (now - timedelta(days=30))
            real_dates = [m.created_at for m in (guest + admin_real)]
            last_at = max(real_dates) if real_dates else conv.created_at
            historical = newest_guest is None or newest_guest < hist_cutoff

            dirty = []
            if conv.is_replied != is_replied:
                conv.is_replied = is_replied
                dirty.append('is_replied')
            if conv.has_unread != has_unread:
                conv.has_unread = has_unread
                dirty.append('has_unread')
            if conv.last_message_at != last_at:
                conv.last_message_at = last_at
                dirty.append('last_message_at')
            if (is_replied or historical) and conv.ai_draft:
                conv.ai_draft = ''
                dirty.append('ai_draft')
                drafts_cleared += 1
            if conv.sentiment == TC.Sentiment.WAITING and historical:
                conv.sentiment = TC.Sentiment.NEUTRAL
                dirty.append('sentiment')
                waiting_neutral += 1

            if dirty:
                repaired += 1
                if commit:
                    conv.save(update_fields=dirty + ['updated_at'])

        # Strip broadcast messages from surviving convs.
        if bc_ids:
            qs = TM.objects.filter(id__in=bc_ids)
            bc_stripped = qs.count()
            if commit:
                qs.delete()

        self.stdout.write(
            '  %s: delete=%s (ages=%s) repair=%s bc_strip=%s (reliable=%s heur=%s) drafts_cleared=%s waiting->neutral=%s'
            % (schema, del_n, dict(buckets), repaired, bc_stripped, bc_reliable, bc_heur, drafts_cleared, waiting_neutral)
        )
        if not commit and not self._no_heuristic and heur_bc:
            ranked = sorted(((len(text_convs[t]), t) for t in heur_bc), reverse=True)[:4]
            for cnt, t in ranked:
                self.stdout.write('      [heur x%s] %r' % (cnt, t[:90]))
        totals['delete'] += del_n
        totals['repair'] += repaired
        totals['bc_strip'] += bc_stripped
        totals['drafts_cleared'] += drafts_cleared
        totals['waiting_neutral'] += waiting_neutral
