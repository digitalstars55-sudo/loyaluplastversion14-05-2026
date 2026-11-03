"""Настройки и оформление подарочных сертификатов (04.11.2026).

Настройки лежат в `ClientConfig.certificate_settings`, картинки и логотип — в
`GiftCertificateDesign` (своя загрузка или четыре варианта от ИИ). На сертификат
картинка не впечатывается: страница получателя кладёт текст поверх неё, поэтому
номинал и код всегда читаются, а логотип стоит в выбранном углу.
"""
import io
import logging

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

УГЛЫ = ('tl', 'tr', 'bl', 'br')
ГДЕ = ('all', 'issue')
НАСТРОЙКИ = {
    'nominals': [1000, 3000, 5000, 10000],
    'period_months': 12,            # 0 — без срока
    'redeem_scope': 'all',          # all — все точки сети, issue — только точка выдачи
    'partial': True,                # списание частями
    'logo_id': None,
    'logo_corner': 'tr',
    'design_id': None,              # картинка по умолчанию
}
МАКС_ФАЙЛ = 10 * 1024 * 1024
МАКС_СТОРОНА = 2000
ВАРИАНТОВ_ИИ = 4
СТИЛИ = {
    'brand': 'тёмный благородный фон, спокойный свет, золотистые акценты',
    'warm': 'тёплый светлый фон, мягкий утренний свет, уютно',
    'fest': 'новогоднее настроение, огоньки, хвоя, без персонажей',
}


class НеверныеНастройки(ValueError):
    pass


def настройки(cfg) -> dict:
    """Настройки сети поверх значений по умолчанию."""
    сохранённые = getattr(cfg, 'certificate_settings', None)
    return {**НАСТРОЙКИ, **(сохранённые if isinstance(сохранённые, dict) else {})}


def проверить_настройки(прежние: dict, правка: dict) -> dict:
    """Новые настройки целиком. Неверное значение — `НеверныеНастройки` с текстом для человека."""
    итог = {**НАСТРОЙКИ, **прежние}
    if 'nominals' in правка:
        try:
            номиналы = sorted({int(x) for x in правка['nominals']})
        except (TypeError, ValueError):
            raise НеверныеНастройки('Номиналы: только целые рубли.')
        if not номиналы or len(номиналы) > 12 or номиналы[0] <= 0 or номиналы[-1] > 1_000_000:
            raise НеверныеНастройки('Номиналы: от одного до двенадцати, каждый от 1 до 1 000 000 ₽.')
        итог['nominals'] = номиналы
    if 'period_months' in правка:
        try:
            месяцы = int(правка['period_months'])
        except (TypeError, ValueError):
            raise НеверныеНастройки('Срок: число месяцев.')
        if месяцы < 0 or месяцы > 120:
            raise НеверныеНастройки('Срок: от 0 (без срока) до 120 месяцев.')
        итог['period_months'] = месяцы
    if 'redeem_scope' in правка:
        if правка['redeem_scope'] not in ГДЕ:
            raise НеверныеНастройки('Где принимают: все точки или точка выдачи.')
        итог['redeem_scope'] = правка['redeem_scope']
    if 'partial' in правка:
        итог['partial'] = bool(правка['partial'])
    if 'logo_corner' in правка:
        if правка['logo_corner'] not in УГЛЫ:
            raise НеверныеНастройки('Логотип: выберите угол.')
        итог['logo_corner'] = правка['logo_corner']
    for ключ in ('logo_id', 'design_id'):
        if ключ in правка:
            значение = правка[ключ]
            if значение in (None, ''):
                итог[ключ] = None
                continue
            try:
                итог[ключ] = int(значение)
            except (TypeError, ValueError):
                raise НеверныеНастройки('Картинка не найдена.')
    return итог


def почему_нельзя_списать(сертификат, сумма: int, точка, cfg) -> tuple[str, str] | None:
    """(код, текст) отказа по настройкам сети, иначе None. Остаток и срок проверяет вызывающий."""
    н = настройки(cfg)
    if not н['partial'] and сумма != сертификат.balance:
        return 'only_full', f'Сеть списывает сертификат только целиком: {сертификат.balance} ₽.'
    if н['redeem_scope'] == 'issue' and сертификат.issued_branch_id:
        if точка is None:
            return 'need_branch', 'Сертификат принимают только в точке выдачи: выберите точку.'
        if точка.pk != сертификат.issued_branch_id:
            return 'wrong_branch', 'Сертификат принимают только в точке, где его выдали.'
    return None


def подготовить_файл(файл, вид: str) -> ContentFile:
    """Проверка и пересборка загрузки: настоящая ли картинка, без EXIF, не шире `МАКС_СТОРОНА`."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if файл.size > МАКС_ФАЙЛ:
        raise НеверныеНастройки('Файл больше 10 МБ.')
    try:
        картинка = Image.open(файл)
        картинка.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise НеверныеНастройки('Это не картинка: нужен JPEG или PNG.')
    картинка = ImageOps.exif_transpose(картинка)
    картинка.thumbnail((МАКС_СТОРОНА, МАКС_СТОРОНА))
    буфер = io.BytesIO()
    # Логотипу нужна прозрачность, картинке нет.
    if вид == 'logo':
        картинка.convert('RGBA').save(буфер, 'PNG', optimize=True)
        return ContentFile(буфер.getvalue(), name='logo.png')
    картинка.convert('RGB').save(буфер, 'JPEG', quality=88, optimize=True)
    return ContentFile(буфер.getvalue(), name='design.jpg')


def задание_ии(пожелание: str, стиль: str, сеть: str) -> str:
    основа = СТИЛИ.get(стиль, СТИЛИ['brand'])
    return (
        f'Иллюстрация для подарочного сертификата заведения «{сеть}». {пожелание}. Настроение: {основа}. '
        'Горизонтальный кадр, спокойная левая половина и центр: поверх неё ляжет текст. '
        'Никакого текста, букв, цифр, логотипов и людей на картинке.'
    )[:900]


def нарисовать_варианты(ids: list[int], задание: str) -> int:
    """Рисует варианты по очереди; сбой одного не роняет остальные. → сколько готово."""
    from apps.tenant.marketer.images import ImageError, _draw

    from .models import GiftCertificateDesign

    готово = 0
    for pk in ids:
        вариант = GiftCertificateDesign.objects.filter(pk=pk).first()
        if вариант is None:
            continue
        try:
            данные, _использовано = _draw(задание, [])
            вариант.image.save(f'ai-{pk}.jpg', ContentFile(данные), save=False)
            вариант.status = GiftCertificateDesign.Status.READY
            вариант.error = ''
            готово += 1
        except ImageError as e:
            вариант.status = GiftCertificateDesign.Status.FAILED
            вариант.error = str(e)[:255]
        except Exception as e:  # noqa: BLE001 — один вариант не должен ронять остальные
            logger.warning('certificate design %s: %s', pk, e)
            вариант.status = GiftCertificateDesign.Status.FAILED
            вариант.error = 'Не получилось нарисовать.'
        вариант.save(update_fields=['image', 'status', 'error'])
    return готово
