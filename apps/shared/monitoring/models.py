from django.db import models


class MonitorAlertState(models.Model):
    """
    Состояние ОДНОГО сигнала мониторинга (ключ = что именно сломано:
    `tls:levelupapp.ru`, `paid:asap_murom`, `vkcb:levone:123`).

    Нужна только ради дедупа: проверки крутятся раз в час, а сертификат
    истекает две недели подряд — без памяти о том, что мы уже писали, владелец
    получил бы 336 одинаковых пушей и выключил уведомления совсем. Поэтому
    храним, когда сигнал впервые увидели (first_seen_at), когда видели в
    последний раз (last_seen_at) и когда в последний раз БУДИЛИ пушем
    (last_sent_at) — повтор уходит не чаще PLATFORM_MONITOR_REPEAT_HOURS.

    resolved_at заполняется, когда сигнал перестал приходить: строка остаётся
    в истории («что у нас ломалось в сентябре»), а не удаляется.
    """

    class Severity(models.TextChoices):
        WARN     = 'warn',     'Предупреждение'
        CRITICAL = 'critical', 'Критично'

    key = models.CharField('Ключ сигнала', max_length=120, unique=True, db_index=True)
    severity = models.CharField('Важность', max_length=10, choices=Severity.choices)
    title = models.CharField('Заголовок', max_length=255)
    body = models.TextField('Текст', blank=True)
    data = models.JSONField('Доп. данные', default=dict, blank=True)

    first_seen_at = models.DateTimeField('Впервые замечен', auto_now_add=True)
    last_seen_at = models.DateTimeField('Замечен в последний раз')
    last_sent_at = models.DateTimeField('Последний пуш', null=True, blank=True)
    resolved_at = models.DateTimeField('Закрыт', null=True, blank=True)
    sent_count = models.PositiveIntegerField('Сколько раз слали', default=0)

    def __str__(self):
        mark = '✅' if self.resolved_at else ('🔴' if self.severity == self.Severity.CRITICAL else '⚠️')
        return f'{mark} {self.key}'

    class Meta:
        verbose_name = 'Сигнал мониторинга'
        verbose_name_plural = 'Сигналы мониторинга'
        ordering = ['-last_seen_at']
