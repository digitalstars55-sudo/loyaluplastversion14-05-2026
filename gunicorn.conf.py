"""Настройки gunicorn, которые нельзя передать в командной строке compose.

Gunicorn сам читает `./gunicorn.conf.py` при старте и при каждом HUP.

17.10.2026: веб упал после HUP — «Too many open files». Лимит открытых
файлов в контейнере 1024 (мягкий) при жёстком 524288; у gthread каждый
запрос держит свои сокеты и epoll, и в пике рабочий процесс и сам master
(с control server gunicorn 26) упирались в 1024. Поднимаем мягкий лимит до
65536 здесь: master поднимает себе, рабочие наследуют при запуске. Без
пересоздания контейнеров (compose менять — только ночью).
"""
import resource

_soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
_want = 65536 if _hard == resource.RLIM_INFINITY else min(65536, _hard)
if _soft < _want:
    resource.setrlimit(resource.RLIMIT_NOFILE, (_want, _hard))
