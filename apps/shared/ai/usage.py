"""Учёт расхода AI: одна строка лога на КАЖДЫЙ вызов.

До этого веб-контейнер вызовов AI не логировал вовсе: сколько съедают
разбор тональности, черновики ответов и генераторы, можно было узнать только
по счёту провайдера — задним числом и без разбивки по местам.

Формат одной строки (logger `ai.usage`, уровень INFO):
    ai.usage where=draft model=gpt-6-luna in=812 out=143 cache_write=0 cache_read=5120 tenant=levone

`cache_read` > 0 — кэш промпта сработал. Ошибка учёта никогда не роняет сам вызов.
"""
import logging

logger = logging.getLogger('ai.usage')


def log_usage(where: str, message) -> None:
    try:
        u = getattr(message, 'usage', None)
        if u is None:
            return
        try:
            from django.db import connection
            tenant = getattr(getattr(connection, 'tenant', None), 'schema_name', '') or ''
        except Exception:
            tenant = ''
        logger.info(
            'ai.usage where=%s model=%s in=%s out=%s cache_write=%s cache_read=%s tenant=%s',
            where, getattr(message, 'model', ''),
            getattr(u, 'input_tokens', 0) or 0, getattr(u, 'output_tokens', 0) or 0,
            getattr(u, 'cache_creation_input_tokens', 0) or 0,
            getattr(u, 'cache_read_input_tokens', 0) or 0,
            tenant,
        )
    except Exception:  # учёт — не повод ронять ответ гостю
        pass


def cached_system(text: str) -> list:
    """Системный промпт одним блоком с точкой кэша.

    Кэш срабатывает, только если префикс не короче минимума модели (у Haiku 4.5 —
    4096 токенов); короче — API молча считает без кэша, ошибки нет. Поэтому
    ставим всегда: у сетей с большой базой знаний (черновик ответа) повторный
    вызов в пределах 5 минут читает промпт из кэша.
    """
    return [{'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral'}}]
