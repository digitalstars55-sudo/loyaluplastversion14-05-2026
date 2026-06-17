"""
Analytics serializers — request validation and response shaping.
"""
from datetime import date, timedelta

from rest_framework import serializers


# ── Request ───────────────────────────────────────────────────────────────────

class StatsQuerySerializer(serializers.Serializer):
    """
    Query params for the general stats endpoint.

    branch_ids — comma-separated Branch PKs, e.g. "1,2,3".
                 Omit (or empty) to aggregate all branches.
    period     — preset shortcut (today | 7d | 30d | 90d | year | all).
                 Ignored when start/end are provided explicitly.
    start      — YYYY-MM-DD
    end        — YYYY-MM-DD
    """
    PERIOD_CHOICES = ['today', '7d', '30d', '90d', 'year', 'all']

    branch_ids = serializers.CharField(required=False, allow_blank=True, default='')
    period     = serializers.ChoiceField(choices=PERIOD_CHOICES, required=False, default='30d')
    start      = serializers.DateField(required=False)
    end        = serializers.DateField(required=False)

    def validate_branch_ids(self, value: str) -> list[int]:
        if not value:
            return []
        try:
            return [int(x.strip()) for x in value.split(',') if x.strip()]
        except ValueError:
            raise serializers.ValidationError('branch_ids must be comma-separated integers.')

    def validate(self, attrs):
        today = date.today()

        if 'start' in attrs and 'end' in attrs:
            if attrs['start'] > attrs['end']:
                raise serializers.ValidationError('start must be before end.')
            return attrs

        # Resolve preset → start/end
        period = attrs.get('period', '30d')
        if period == 'today':
            attrs['start'] = attrs['end'] = today
        elif period == '7d':
            attrs['start'] = today - timedelta(days=6)
            attrs['end']   = today
        elif period == '30d':
            attrs['start'] = today - timedelta(days=29)
            attrs['end']   = today
        elif period == '90d':
            attrs['start'] = today - timedelta(days=89)
            attrs['end']   = today
        elif period == 'year':
            attrs['start'] = today.replace(month=1, day=1)
            attrs['end']   = today
        elif period == 'all':
            attrs['start'] = date(2000, 1, 1)
            attrs['end']   = today

        return attrs


class RFQuerySerializer(serializers.Serializer):
    """Query params for RF analysis endpoint."""
    PERIOD_CHOICES = ['today', '7d', '30d', '90d', 'year', 'all']

    branch_ids = serializers.CharField(required=False, allow_blank=True, default='')
    trend_days = serializers.IntegerField(min_value=7, max_value=365, default=30)
    mode       = serializers.ChoiceField(choices=['restaurant', 'delivery'], default='restaurant')
    r_score    = serializers.IntegerField(min_value=1, max_value=10, required=False)
    f_score    = serializers.IntegerField(min_value=1, max_value=10, required=False)
    period     = serializers.ChoiceField(choices=PERIOD_CHOICES, required=False, default='30d')
    start      = serializers.DateField(required=False)
    end        = serializers.DateField(required=False)

    def validate_branch_ids(self, value: str) -> list[int]:
        if not value:
            return []
        try:
            return [int(x.strip()) for x in value.split(',') if x.strip()]
        except ValueError:
            raise serializers.ValidationError('branch_ids must be comma-separated integers.')

    def validate(self, attrs):
        today = date.today()

        if 'start' in attrs and 'end' in attrs:
            if attrs['start'] > attrs['end']:
                raise serializers.ValidationError('start must be before end.')
            return attrs

        period = attrs.get('period', '30d')
        if period == 'today':
            attrs['start'] = attrs['end'] = today
        elif period == '7d':
            attrs['start'] = today - timedelta(days=6)
            attrs['end']   = today
        elif period == '30d':
            attrs['start'] = today - timedelta(days=29)
            attrs['end']   = today
        elif period == '90d':
            attrs['start'] = today - timedelta(days=89)
            attrs['end']   = today
        elif period == 'year':
            attrs['start'] = today.replace(month=1, day=1)
            attrs['end']   = today
        elif period == 'all':
            attrs['start'] = date(2000, 1, 1)
            attrs['end']   = today

        return attrs


# ── Response ──────────────────────────────────────────────────────────────────

class BranchSerializer(serializers.Serializer):
    id   = serializers.IntegerField()
    name = serializers.CharField()


class GeneralStatsSerializer(serializers.Serializer):
    qr_scans                  = serializers.IntegerField()
    cafe_scans                = serializers.IntegerField(required=False)
    delivery_scans            = serializers.IntegerField(required=False)
    total_scans               = serializers.IntegerField(required=False)
    game_reached              = serializers.IntegerField(required=False)
    total_vk_subscribers      = serializers.IntegerField()
    new_group_with_gift       = serializers.IntegerField()
    repeat_game_players       = serializers.IntegerField()
    coin_purchasers           = serializers.IntegerField()
    new_community_subscribers = serializers.IntegerField()
    new_newsletter_subscribers = serializers.IntegerField()
    birthday_greetings_sent   = serializers.IntegerField()
    birthday_celebrants       = serializers.IntegerField()
    message_open_rate         = serializers.FloatField()
    vk_stories_publishers     = serializers.IntegerField()
    stories_referrals         = serializers.IntegerField()
    pos_guests                = serializers.IntegerField()
    scan_index                = serializers.FloatField()


class ChartDataSerializer(serializers.Serializer):
    repeat_visits    = serializers.DictField()
    gift_sources     = serializers.DictField()
    staff_involvement = serializers.DictField()
    quests           = serializers.DictField()


class GeneralStatsResponseSerializer(serializers.Serializer):
    stats  = GeneralStatsSerializer()
    charts = ChartDataSerializer()
    meta   = serializers.DictField()


class RFSegmentSerializer(serializers.Serializer):
    id       = serializers.IntegerField()
    code     = serializers.CharField()
    name     = serializers.CharField()
    emoji    = serializers.CharField()
    color    = serializers.CharField()
    count    = serializers.IntegerField()
    strategy = serializers.CharField()


class RFStatsResponseSerializer(serializers.Serializer):
    segments   = RFSegmentSerializer(many=True)
    trend      = serializers.ListField()
    migrations = serializers.ListField()
