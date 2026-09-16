"""
JSON-API рассылок для внешнего кабинета CheckUp (контракт платформы №11/№13).

Пакет живёт ПОВЕРХ существующей механики рассылок и ничего в ней не меняет:
модели Broadcast/BroadcastSend/BroadcastRecipient, сервисы senler.services и
celery-задача run_broadcast_task используются как есть. Путь мобильного
приложения (analytics.api.views.SendSegmentBroadcastAPIView) не затронут —
его логика здесь ПРОДУБЛИРОВАНА, чтобы правки внешнего API физически не
могли сломать живую отправку.
"""
