"""
Проверки платформы. Каждая — чистая функция: всё, что ходит наружу (сеть) и
всё, что зависит от «сегодня», передаётся параметром. Отсюда два следствия,
ради которых так и сделано:

  * тесты гоняются без сети, без БД и без Django-настроек — подставил фейковый
    fetch_* и любую дату, получил список сигналов;
  * settings здесь НЕ читаются вообще. Кто откуда берёт хосты и списки — дело
    tasks.py, а проверки остаются переиспользуемыми (например, из shell).

Проверка НИКОГДА не падает целиком из-за одного объекта: упал один хост —
остальные всё равно проверяем, а про упавший рождается отдельный сигнал
(«сертификат не проверить» — это тоже беда, а не повод молчать).
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger(__name__)

SEVERITY_WARN = 'warn'
SEVERITY_CRITICAL = 'critical'

# Порог, ниже которого «скоро истечёт» превращается в «горит»: за три дня
# перевыпустить сертификат/продлить домен ещё реально, за один — уже нет.
CRITICAL_DAYS = 3

# По этой подстроке в url отличаем НАШИ callback-серверы в ВК от чужих:
# у части сетей в той же группе живёт callback Senler.ru — его чиним не мы,
# и падать из-за него сигналом нельзя.
OUR_CALLBACK_MARKER = 'levelupapp.ru'

VK_API_VERSION = '5.131'
WHOIS_HOST = 'whois.tcinet.ru'
WHOIS_PORT = 43
NET_TIMEOUT = 10


@dataclass
class Finding:
    """
    Один сигнал. key — стабильный идентификатор проблемы (по нему alerts.py
    понимает «это та же самая беда, что вчера», и не будит повторно).
    data — машинные подробности для админки и для разбора постфактум.
    """
    key: str
    severity: str
    title: str
    body: str
    data: dict = field(default_factory=dict)


def _severity_by_days(days_left: int, warn_days: int) -> str | None:
    """Общая шкала для всех сроков: истёк/≤3 дней — критично, ≤warn_days — предупреждение."""
    if days_left <= CRITICAL_DAYS:
        return SEVERITY_CRITICAL
    if days_left <= warn_days:
        return SEVERITY_WARN
    return None


def _days_word(n: int) -> str:
    """«1 день / 2 дня / 5 дней» — пуш читает человек, а не грепает робот."""
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return 'дней'
    last = n % 10
    if last == 1:
        return 'день'
    if last in (2, 3, 4):
        return 'дня'
    return 'дней'


# ── 1. TLS-сертификаты ────────────────────────────────────────────────────────

def fetch_tls_not_after(host: str) -> date:
    """
    Дата окончания сертификата, как её отдаёт сам хост. Ходим обычным
    TLS-хендшейком с проверкой цепочки: если сертификат просрочен или подменён,
    wrap_socket бросит — и это ровно тот сигнал, который нам нужен.
    """
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=NET_TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            not_after = tls.getpeercert()['notAfter']
    # Формат OpenSSL: 'Nov 20 12:00:00 2026 GMT'
    return datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z').date()


def check_tls(hosts, today: date, warn_days: int, fetch_not_after=None) -> list[Finding]:
    """
    Сертификаты наших доменов. Ноябрь 2025 научил: истёкший wildcard кладёт
    вход у ВСЕХ сетей одновременно, а авто-renew мог тихо не сработать месяц
    назад. Поэтому смотрим живой сертификат на самом хосте, а не бумажки.
    """
    fetch = fetch_not_after or fetch_tls_not_after
    findings: list[Finding] = []

    for host in hosts:
        try:
            not_after = fetch(host)
        except Exception as e:
            findings.append(Finding(
                key=f'tls:{host}:error',
                severity=SEVERITY_CRITICAL,
                title=f'Сертификат {host} не проверить',
                body=f'Не удалось получить сертификат {host}: {e}. Хост может не отвечать вовсе.',
                data={'host': host, 'error': str(e)},
            ))
            continue

        days_left = (not_after - today).days
        severity = _severity_by_days(days_left, warn_days)
        if severity is None:
            continue

        when = not_after.strftime('%d.%m.%Y')
        if days_left < 0:
            body = (f'Сертификат {host} истёк {when} ({abs(days_left)} {_days_word(days_left)} назад). '
                    f'Вход не открывается ни у кого — перевыпускать прямо сейчас.')
        else:
            body = (f'Сертификат {host} истекает {when} — осталось {days_left} {_days_word(days_left)}. '
                    f'Проверь авто-renew.')
        findings.append(Finding(
            key=f'tls:{host}',
            severity=severity,
            title=f'TLS {host}: {days_left} {_days_word(days_left)}' if days_left >= 0 else f'TLS {host} истёк',
            body=body,
            data={'host': host, 'not_after': not_after.isoformat(), 'days_left': days_left},
        ))

    return findings


# ── 2. Домены ─────────────────────────────────────────────────────────────────

def _parse_whois_date(raw: str) -> date | None:
    """paid-till приходит то как 2026-12-19T21:00:00Z, то как 2026.12.19."""
    raw = (raw or '').strip()
    if not raw:
        return None
    head = raw.split('T')[0].split(' ')[0].replace('.', '-')
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def whois_paid_till(domain: str) -> date | None:
    """
    Живая дата продления из реестра .RU (whois.tcinet.ru, поле paid-till).
    Свой сокет, а не библиотека: whois — это три строчки TCP, а лишняя
    зависимость на проде дороже. Любая ошибка — None: «не дозвонились до
    реестра» не повод кричать, у нас есть запасная дата из настроек.
    """
    try:
        with socket.create_connection((WHOIS_HOST, WHOIS_PORT), timeout=NET_TIMEOUT) as sock:
            sock.sendall((domain + '\r\n').encode('utf-8'))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        text = b''.join(chunks).decode('utf-8', errors='ignore')
    except Exception as e:
        logger.info('whois %s: %s', domain, e)
        return None

    for line in text.splitlines():
        if line.strip().lower().startswith('paid-till:'):
            return _parse_whois_date(line.split(':', 1)[1])
    return None


# Алиас нужен потому, что одноимённый параметр check_domains (имя закреплено
# сигнатурой) перекрывает функцию внутри тела.
_DEFAULT_WHOIS = whois_paid_till


def check_domains(expiry_fallback: dict, today: date, warn_days: int, whois_paid_till=None) -> list[Finding]:
    """
    Сроки продления доменов. Просроченный levonework.ru в 2026-м выглядел как
    «у всех всё легло», хотя сервер был жив — Timeweb просто подменил сайт
    парковкой. SSH тут не помогает, лечит только владелец, поэтому узнать надо
    заранее.

    Приоритет: живой whois. Если реестр молчит — берём дату из настроек и в
    тексте честно предупреждаем, что она могла устареть после продления.
    """
    lookup = whois_paid_till or _DEFAULT_WHOIS
    findings: list[Finding] = []

    for domain, fallback in (expiry_fallback or {}).items():
        paid_till = None
        try:
            paid_till = lookup(domain)
        except Exception as e:
            logger.info('check_domains: whois %s упал: %s', domain, e)

        source = 'whois'
        if not paid_till:
            source = 'fallback'
            paid_till = _parse_whois_date(str(fallback))

        if not paid_till:
            # Нет ни живой даты, ни валидной запасной — молчим: выдумывать срок
            # хуже, чем не знать его (иначе получим вечный ложный сигнал).
            logger.warning('check_domains: нет даты для %s (fallback=%r)', domain, fallback)
            continue

        days_left = (paid_till - today).days
        severity = _severity_by_days(days_left, warn_days)
        if severity is None:
            continue

        when = paid_till.strftime('%d.%m.%Y')
        if days_left < 0:
            body = (f'Домен {domain} не оплачен с {when} — сайт отдаёт заглушку регистратора. '
                    f'Чинится только продлением у регистратора.')
        else:
            body = f'Домен {domain} оплачен до {when} — осталось {days_left} {_days_word(days_left)}.'
        if source == 'fallback':
            body += ' Дата из настроек — сверь после продления.'

        findings.append(Finding(
            key=f'domain:{domain}',
            severity=severity,
            title=f'Домен {domain}: {days_left} {_days_word(days_left)}' if days_left >= 0 else f'Домен {domain} просрочен',
            body=body,
            data={'domain': domain, 'paid_till': paid_till.isoformat(), 'days_left': days_left, 'source': source},
        ))

    return findings


# ── 3. Оплата сетей ───────────────────────────────────────────────────────────

def check_tenants_paid(companies, today: date, warn_days: int) -> list[Finding]:
    """
    Оплата тенантов (Company.paid_until). public — не сеть, выключенные сети
    уже никому не мешают, пустой paid_until = «срок не ведём» (бесплатный
    доступ, dev-схемы) — это не повод будить.
    """
    findings: list[Finding] = []

    for company in companies:
        schema_name = getattr(company, 'schema_name', '')
        if schema_name == 'public' or not getattr(company, 'is_active', False):
            continue
        paid_until = getattr(company, 'paid_until', None)
        if not paid_until:
            continue

        days_left = (paid_until - today).days
        name = getattr(company, 'name', '') or schema_name
        when = paid_until.strftime('%d.%m.%Y')

        if days_left < 0:
            severity = SEVERITY_CRITICAL
            body = (f'{name} ({schema_name}): оплата истекла {abs(days_left)} {_days_word(days_left)} назад '
                    f'({when}). Фоновые задачи остановятся, когда включат гард.')
            title = f'Оплата истекла: {name}'
        elif days_left <= warn_days:
            severity = SEVERITY_WARN
            body = f'{name} ({schema_name}): оплата до {when} — осталось {days_left} {_days_word(days_left)}.'
            title = f'Оплата {name}: {days_left} {_days_word(days_left)}'
        else:
            continue

        findings.append(Finding(
            key=f'paid:{schema_name}',
            severity=severity,
            title=title,
            body=body,
            data={
                'schema_name': schema_name,
                'name': name,
                'paid_until': paid_until.isoformat(),
                'days_left': days_left,
            },
        ))

    return findings


# ── 4. Callback ВК ────────────────────────────────────────────────────────────

def fetch_vk_callback_servers(group_id, token: str) -> list[dict]:
    """groups.getCallbackServers → список серверов группы (url, status, title)."""
    params = urllib.parse.urlencode({
        'group_id': group_id,
        'access_token': token,
        'v': VK_API_VERSION,
    })
    url = f'https://api.vk.com/method/groups.getCallbackServers?{params}'
    with urllib.request.urlopen(url, timeout=NET_TIMEOUT) as resp:
        data = json.loads(resp.read())
    if 'error' in data:
        raise RuntimeError(data['error'].get('error_msg', 'VK API error'))
    return (data.get('response') or {}).get('items') or []


def check_vk_callbacks(tenant_groups, fetch_servers) -> list[Finding]:
    """
    Живость наших callback-серверов в ВК.

    ВК отключает callback сам, молча, когда сервер начинает отвечать не 200
    (сентябрь 2026: всплеск ретраев поймал 429 от общего лимита nginx — отвалились
    пять сетей сразу, узнали через сутки). После отключения ВК перестаёт слать
    сообщения гостей вовсе: ни отзывов, ни ответов.

    tenant_groups — (schema_name, group_id, token). Смотрим ТОЛЬКО серверы с
    нашим доменом в url: чужой callback (Senler и прочие партнёры) чиним не мы.
    Ошибку самого вызова считаем warn, а не critical: протухший токен или
    пятиминутная недоступность ВК — не то же самое, что отключённый callback.
    """
    findings: list[Finding] = []

    for schema_name, group_id, token in tenant_groups:
        try:
            servers = fetch_servers(group_id, token) or []
        except Exception as e:
            findings.append(Finding(
                key=f'vkcb:{schema_name}:{group_id}:error',
                severity=SEVERITY_WARN,
                title=f'Callback ВК не проверить: {schema_name}',
                body=f'{schema_name} (группа {group_id}): не удалось спросить ВК — {e}. '
                     f'Чаще всего протух токен сообщества.',
                data={'schema_name': schema_name, 'group_id': group_id, 'error': str(e)},
            ))
            continue

        ours = [s for s in servers if OUR_CALLBACK_MARKER in (s.get('url') or '')]
        broken = [s for s in ours if s.get('status') != 'ok']
        if not broken:
            continue

        details = ', '.join(
            f"{s.get('title') or s.get('url') or '?'} — {s.get('status') or '?'}"
            for s in broken
        )
        findings.append(Finding(
            key=f'vkcb:{schema_name}:{group_id}',
            severity=SEVERITY_CRITICAL,
            title=f'Callback ВК отключён: {schema_name}',
            body=f'{schema_name} (группа {group_id}): {details}. '
                 f'Сообщения гостей и отзывы из ВК не доходят — реактивировать через editCallbackServer.',
            data={
                'schema_name': schema_name,
                'group_id': group_id,
                'servers': [
                    {'url': s.get('url'), 'status': s.get('status'), 'title': s.get('title')}
                    for s in broken
                ],
            },
        ))

    return findings


# ── 5. Доступность входа ──────────────────────────────────────────────────────

def fetch_probe_status(url: str) -> int:
    """
    HTTP-код адреса. HTTPError ловим отдельно и отдаём его код: «ответил 502»
    читается и чинится проще, чем голый текст исключения.
    """
    request = urllib.request.Request(url, headers={'User-Agent': 'LoyalUP-Monitor/1'})
    try:
        with urllib.request.urlopen(request, timeout=NET_TIMEOUT) as resp:
            return int(resp.getcode())
    except urllib.error.HTTPError as e:
        return int(e.code)


def check_probes(urls, fetch_status=None) -> list[Finding]:
    """
    Живые адреса входа: бутстрап мини-аппа и API тенанта. Сертификат и домен
    могут быть в порядке, а гость всё равно упирается в 502 — это единственная
    проверка, которая смотрит на систему глазами гостя.
    """
    fetch = fetch_status or fetch_probe_status
    findings: list[Finding] = []

    for url in urls:
        try:
            status = fetch(url)
        except Exception as e:
            findings.append(Finding(
                key=f'probe:{url}',
                severity=SEVERITY_CRITICAL,
                title=f'Не отвечает: {url}',
                body=f'{url} не отвечает: {e}. Для гостя это пустой экран.',
                data={'url': url, 'error': str(e)},
            ))
            continue

        if status != 200:
            findings.append(Finding(
                key=f'probe:{url}',
                severity=SEVERITY_CRITICAL,
                title=f'{url} отдаёт {status}',
                body=f'{url} ответил {status} вместо 200. Для гостя это пустой экран.',
                data={'url': url, 'status': status},
            ))

    return findings
