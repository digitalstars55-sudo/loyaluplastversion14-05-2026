"""Карта гостя в Wallet (слово владельца 01.11.2026: сравняться с Remarked).

Apple: файл .pkpass, подпись PKCS#7 сертификатом Pass Type ID (`WALLET_PASS_*`). На Android тот
же файл открывают приложения вида WalletPasses. Баланс обновляется сам: телефон регистрируется
в нашей веб-службе, при движении монет уходит пуш APNs, телефон забирает свежий файл.
Google Wallet: ссылка «сохранить» с подписью сервисного аккаунта (`WALLET_GOOGLE_*`).

Включается флагом сети `wallet_enabled` и только когда есть ключи: без них карта не предлагается.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
import struct
import time
import zipfile
import zlib
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

log = logging.getLogger(__name__)

ФОН, ТЕКСТ, ПОДПИСЬ = 'rgb(27,40,56)', 'rgb(255,255,255)', 'rgb(232,184,45)'
ЦВЕТ_ЗНАЧКА = (27, 40, 56)


def _настройка(имя: str) -> str:
    return str(getattr(settings, имя, '') or '')


def apple_готов() -> bool:
    return all(_настройка(к) for к in ('WALLET_PASS_TYPE_ID', 'WALLET_TEAM_ID', 'WALLET_PASS_CERT', 'WALLET_PASS_KEY', 'WALLET_WWDR_CERT'))


def google_готов() -> bool:
    return all(_настройка(к) for к in ('WALLET_GOOGLE_ISSUER_ID', 'WALLET_GOOGLE_SA_JSON'))


def _конфиг():
    tenant = getattr(connection, 'tenant', None)
    return getattr(tenant, 'config', None) if tenant is not None else None


def включён() -> bool:
    cfg = _конфиг()
    return bool(cfg is not None and getattr(cfg, 'wallet_enabled', False) and (apple_готов() or google_готов()))


def _сеть() -> str:
    tenant = getattr(connection, 'tenant', None)
    return str(getattr(tenant, 'name', '') or 'Программа лояльности')


def карта_гостя(client, host: str = ''):
    from .models import WalletPass
    wp = WalletPass.objects.filter(client=client).first()
    if wp is None:
        wp, _ = WalletPass.objects.get_or_create(client=client, defaults={'serial': secrets.token_hex(16), 'auth_token': secrets.token_hex(24), 'host': host})
    elif host and wp.host != host:
        wp.host = host
        wp.save(update_fields=['host'])
    return wp


def баланс(client) -> int:
    from .models import ClientBranch
    return sum(cb.coins_balance for cb in ClientBranch.objects.filter(client=client))


def код_на_кассе(client) -> str:
    """Что сканирует кассир: номер карты iikoCard, если он выдан. Иначе карта без штрихкода."""
    from apps.tenant.analytics.models import PosGuestLink
    link = PosGuestLink.objects.filter(client=client).first()
    return (link.card_number if link else '') or ''


def _точки_рядом() -> list:
    """Подсказка на экране блокировки у точки сети. Координат нет: подсказки нет."""
    from .models import Branch
    вышло = []
    for b in Branch.objects.filter(is_active=True)[:10]:
        lat, lon = getattr(b, 'latitude', None), getattr(b, 'longitude', None)
        if lat and lon:
            вышло.append({'latitude': float(lat), 'longitude': float(lon), 'relevantText': f'Вы рядом с {b.name}. Покажите карту на кассе'})
    return вышло


def содержимое(client, wp) -> dict:
    имя = ' '.join(x for x in (client.first_name, client.last_name) if x).strip()
    код = код_на_кассе(client)
    сеть = _сеть()
    pass_json = {
        'formatVersion': 1, 'passTypeIdentifier': _настройка('WALLET_PASS_TYPE_ID'), 'teamIdentifier': _настройка('WALLET_TEAM_ID'),
        'serialNumber': wp.serial, 'organizationName': сеть, 'description': f'Карта гостя {сеть}', 'logoText': сеть,
        'backgroundColor': ФОН, 'foregroundColor': ТЕКСТ, 'labelColor': ПОДПИСЬ,
        'storeCard': {
            'primaryFields': [{'key': 'balance', 'label': 'Баллы', 'value': баланс(client), 'changeMessage': 'Теперь у вас %@ баллов'}],
            'secondaryFields': [{'key': 'name', 'label': 'Гость', 'value': имя or 'Гость'}],
            'backFields': [{'key': 'about', 'label': 'Как пользоваться', 'value': 'Покажите карту на кассе. Баллы обновляются сами.'}],
        },
    }
    if wp.host:
        pass_json['webServiceURL'] = f'https://{wp.host}/api/v1/wallet/apple'
        pass_json['authenticationToken'] = wp.auth_token
    if код:
        pass_json['barcodes'] = [{'format': 'PKBarcodeFormatQR', 'message': код, 'messageEncoding': 'iso-8859-1', 'altText': код}]
    точки = _точки_рядом()
    if точки:
        pass_json['locations'] = точки
    return pass_json


def _png(размер: int, цвет=ЦВЕТ_ЗНАЧКА) -> bytes:
    """Простой значок без сторонних библиотек: Wallet требует icon.png."""
    сырьё = b''.join(b'\x00' + bytes(цвет) * размер for _ in range(размер))
    def кусок(тип, данные):
        return struct.pack('>I', len(данные)) + тип + данные + struct.pack('>I', zlib.crc32(тип + данные) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + кусок(b'IHDR', struct.pack('>IIBBBBB', размер, размер, 8, 2, 0, 0, 0)) + кусок(b'IDAT', zlib.compress(сырьё)) + кусок(b'IEND', b'')


def _картинки() -> dict:
    файлы = {'icon.png': _png(29), 'icon@2x.png': _png(58), 'icon@3x.png': _png(87)}
    cfg = _конфиг()
    logo = getattr(cfg, 'logotype_image', None) if cfg is not None else None
    try:
        if logo and logo.name and logo.name.lower().endswith('.png'):
            with logo.open('rb') as f:
                файлы['logo.png'] = f.read()
    except OSError:
        log.warning('wallet: логотип сети не прочитался')
    return файлы


def подпись(manifest: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7
    серт = x509.load_pem_x509_certificate(Path(_настройка('WALLET_PASS_CERT')).read_bytes())
    ключ = serialization.load_pem_private_key(Path(_настройка('WALLET_PASS_KEY')).read_bytes(), password=None)
    wwdr = x509.load_pem_x509_certificate(Path(_настройка('WALLET_WWDR_CERT')).read_bytes())
    return (pkcs7.PKCS7SignatureBuilder().set_data(manifest).add_signer(серт, ключ, hashes.SHA256()).add_certificate(wwdr)
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]))


def собрать_pkpass(client, wp) -> bytes:
    файлы = {'pass.json': json.dumps(содержимое(client, wp), ensure_ascii=False).encode(), **_картинки()}
    manifest = json.dumps({имя: hashlib.sha1(данные).hexdigest() for имя, данные in файлы.items()}).encode()  # noqa: S324 — так требует формат
    буфер = io.BytesIO()
    with zipfile.ZipFile(буфер, 'w', zipfile.ZIP_DEFLATED) as z:
        for имя, данные in файлы.items():
            z.writestr(имя, данные)
        z.writestr('manifest.json', manifest)
        z.writestr('signature', подпись(manifest))
    return буфер.getvalue()


def ссылка_google(client, wp) -> str:
    """Ссылка «сохранить в Google Wallet». Баланс в ней на момент выдачи."""
    if not google_готов():
        return ''
    import jwt
    sa = json.loads(Path(_настройка('WALLET_GOOGLE_SA_JSON')).read_text())
    выпускающий = _настройка('WALLET_GOOGLE_ISSUER_ID')
    схема = getattr(connection, 'schema_name', 'public')
    класс = f'{выпускающий}.loyalup_{схема}'.replace('-', '_')
    сеть = _сеть()
    объект = {
        'id': f'{выпускающий}.{wp.serial}', 'classId': класс, 'state': 'ACTIVE', 'accountId': wp.serial[:16],
        'accountName': ' '.join(x for x in (client.first_name, client.last_name) if x).strip() or 'Гость',
        'loyaltyPoints': {'label': 'Баллы', 'balance': {'int': баланс(client)}},
    }
    код = код_на_кассе(client)
    if код:
        объект['barcode'] = {'type': 'QR_CODE', 'value': код, 'alternateText': код}
    claims = {'iss': sa['client_email'], 'aud': 'google', 'typ': 'savetowallet', 'iat': int(time.time()),
              'origins': [f'https://{wp.host}'] if wp.host else [],
              'payload': {'loyaltyClasses': [{'id': класс, 'issuerName': сеть, 'programName': f'Карта гостя {сеть}', 'reviewStatus': 'UNDER_REVIEW', 'hexBackgroundColor': '#1B2838',
                                              'programLogo': {'sourceUri': {'uri': f'https://{wp.host}/static/images/LoyalUpLogo.png'}}}],
                          'loyaltyObjects': [объект]}}
    return 'https://pay.google.com/gp/v/save/' + jwt.encode(claims, sa['private_key'], algorithm='RS256')


def отметить_изменение(client_id) -> None:
    """Баллы изменились: карте новая отметка времени и пуш телефонам. Карты нет: ничего не делаем."""
    from .models import WalletPass
    обновлено = WalletPass.objects.filter(client_id=client_id).update(changed_at=timezone.now())
    if обновлено:
        from .tasks import wallet_push
        wallet_push.delay(connection.schema_name, client_id)


def пуш_apns(wp) -> int:
    """Пустой пуш APNs: телефон сам заберёт новую карту. Нужен h2; нет его: пропускаем, карта
    обновится при ручном обновлении в Wallet."""
    try:
        import httpx
        import h2  # noqa: F401
    except ImportError:
        log.warning('wallet: нет h2, пуш пропущен')
        return 0
    if not apple_готов():
        return 0
    дошло = 0
    with httpx.Client(http2=True, timeout=10, cert=(_настройка('WALLET_PASS_CERT'), _настройка('WALLET_PASS_KEY'))) as c:
        for d in wp.devices.all():
            try:
                r = c.post(f'https://api.push.apple.com/3/device/{d.push_token}', headers={'apns-topic': _настройка('WALLET_PASS_TYPE_ID')}, json={})
            except httpx.HTTPError as e:
                log.warning('wallet: APNs %s', e)
                continue
            if r.status_code == 200:
                дошло += 1
            elif r.status_code == 410:
                d.delete()
    return дошло
