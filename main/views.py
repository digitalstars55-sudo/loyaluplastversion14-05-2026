from django.http import JsonResponse


def health(request):
    """
    Лёгкий health-check для «гирлянды» (failover-выбор доступной точки входа).

    Назначение: фронт мини-аппа при запуске пробивает несколько доменов-«дверей»
    (Yandex Cloud / Timeweb / CF), и по первой ответившей 200 идёт дальше. Это
    позволяет работать без VPN — выбирается «белая» доступная точка.

    Намеренно без БД и авторизации: только подтверждение, что бэкенд за этой
    дверью отвечает. Быстро и дёшево.
    """
    resp = JsonResponse({'ok': True})
    resp['Cache-Control'] = 'no-store'
    return resp
