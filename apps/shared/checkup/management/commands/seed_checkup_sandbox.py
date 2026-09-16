# -*- coding: utf-8 -*-
"""
Песочница для стыка с CheckUp (контракт платформы, раздел 7).

Наполняет сеть dev тем, что нужно агенту CheckUp, чтобы начать: вторая точка
(на одной точке не проверить «сотрудник одной точки не видит другую»),
учётка checkup-sandbox для веб-кабинета и десяток тихих тестовых отзывов на
обе точки. «Тихих» — потому что штатный путь создания отзыва зовёт разбор ИИ
(кредиты Anthropic) и пуш сотрудникам; здесь строки кладутся напрямую, с
уже проставленной тональностью и has_unread=False.

Идемпотентно: повторный запуск ничего не дублирует. Без --commit только
показывает план.
"""
import secrets
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

from apps.shared.users.models import User

SANDBOX_BRANCH_ID = 990002          # публичный branch_id второй точки; нарочно непохож на живые
SANDBOX_BRANCH_NAME = 'Песочница CheckUp · Точка 2'
SANDBOX_USERNAME = 'checkup-sandbox'
SANDBOX_MARK = '[песочница CheckUp]'
SANDBOX_VK_BASE = 990_000_000       # vk_sender_id тестовых гостей: 990000001, 990000002, …

# (оценка, текст, стол, ответить ли администратором)
REVIEWS = [
    (5, 'Очень вкусные роллы, официант Иван — молодец.', 3, True),
    (2, 'Ждали заказ 40 минут, суп принесли холодным.', 7, False),
    (4, 'Хорошее место, но громкая музыка.', 1, True),
    (1, 'В салате волос. Больше не придём.', 12, False),
    (5, 'Лучшая пицца в городе!', 5, False),
    (3, 'Нормально, без восторга.', 2, False),
    (2, 'Кассир нахамил на выдаче.', 4, True),
    (4, 'Быстро, вкусно, чисто.', 9, False),
    (5, 'Спасибо за подарок на день рождения.', 6, True),
    (1, 'Заказ перепутали дважды.', 8, False),
]


def _sentiment(rating: int) -> str:
    if rating >= 4:
        return 'POSITIVE'
    if rating == 3:
        return 'NEUTRAL'
    return 'NEGATIVE'


class Command(BaseCommand):
    help = 'Наполняет песочницу dev для стыка с CheckUp: вторая точка, учётка checkup-sandbox, тихие тестовые отзывы.'

    def add_arguments(self, parser):
        parser.add_argument('--schema', default='dev', help='Сеть-песочница (по умолчанию dev)')
        parser.add_argument('--commit', action='store_true', help='Реально писать в базу')
        parser.add_argument('--reset-password', action='store_true',
                            help='Выдать checkup-sandbox новый пароль (печатается один раз)')

    def handle(self, *args, **opts):
        schema = opts['schema']
        commit = opts['commit']
        if schema in ('public', 'levone') or schema.startswith('asap'):
            raise CommandError(f'{schema} — живая сеть, песочницу там не заводим')

        Tenant = get_tenant_model()
        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(f'сети {schema} нет')
        if not tenant.is_active:
            raise CommandError(f'сеть {schema} выключена')

        from apps.tenant.branch.models import Branch, TestimonialConversation, TestimonialMessage

        with schema_context(schema):
            branches = list(Branch.objects.order_by('id'))
            second = Branch.objects.filter(branch_id=SANDBOX_BRANCH_ID).first()
            self.stdout.write(f'сеть {schema}: точек {len(branches)} → {[(b.id, b.branch_id, b.name) for b in branches]}')

            if second is None:
                self.stdout.write(f'  + точка branch_id={SANDBOX_BRANCH_ID} «{SANDBOX_BRANCH_NAME}»')
                if commit:
                    second = Branch.objects.create(
                        branch_id=SANDBOX_BRANCH_ID, name=SANDBOX_BRANCH_NAME, is_active=True,
                        description='Тестовая точка для стыка с CheckUp (контракт платформы, §7). Не для гостей.',
                    )
            else:
                self.stdout.write(f'  = точка {SANDBOX_BRANCH_ID} уже есть (id={second.id})')

            first = next((b for b in branches if b.branch_id != SANDBOX_BRANCH_ID), None)
            if first is None:
                raise CommandError('в сети нет ни одной «первой» точки — странно, останавливаюсь')

            have = TestimonialMessage.objects.filter(source='APP', text__startswith=SANDBOX_MARK).count()
            self.stdout.write(f'  отзывов песочницы: {have} из {len(REVIEWS)}')
            if have < len(REVIEWS) and (second is not None or not commit):
                now = timezone.now()
                for i, (rating, text, table, answered) in enumerate(REVIEWS):
                    if i < have:
                        continue
                    branch = first if i % 2 == 0 else second
                    when = now - timedelta(days=13 - i, hours=i * 2)
                    self.stdout.write(f'  + отзыв #{i + 1} ★{rating} → {branch.name if branch else "?"} стол {table}'
                                      f'{" + ответ" if answered else ""}')
                    if not commit:
                        continue
                    conv = TestimonialConversation.objects.create(
                        branch=branch, vk_sender_id=str(SANDBOX_VK_BASE + i + 1),
                        sentiment=_sentiment(rating), has_unread=False, is_replied=answered,
                        last_message_at=when,
                    )
                    msg = TestimonialMessage.objects.create(
                        conversation=conv, source='APP', text=f'{SANDBOX_MARK} {text}',
                        rating=rating, table_number=table, branch=branch,
                    )
                    TestimonialMessage.objects.filter(pk=msg.pk).update(created_at=when)
                    if answered:
                        reply = TestimonialMessage.objects.create(
                            conversation=conv, source='ADMIN_REPLY',
                            text='Спасибо за отзыв! Разобрались, ждём вас снова.', branch=branch,
                        )
                        TestimonialMessage.objects.filter(pk=reply.pk).update(created_at=when + timedelta(hours=1))
                    TestimonialConversation.objects.filter(pk=conv.pk).update(created_at=when)

        user = User.objects.filter(username=SANDBOX_USERNAME).first()
        new_password = None
        if user is None:
            new_password = secrets.token_urlsafe(12)
            self.stdout.write(f'  + пользователь {SANDBOX_USERNAME} (network_admin, сеть {schema})')
            if commit:
                user = User.objects.create_user(username=SANDBOX_USERNAME, password=new_password,
                                                role='network_admin', first_name='Песочница CheckUp')
        else:
            self.stdout.write(f'  = пользователь {SANDBOX_USERNAME} уже есть (id={user.id})')
            if opts['reset_password']:
                new_password = secrets.token_urlsafe(12)
                if commit:
                    user.set_password(new_password)
                    user.save(update_fields=['password'])
        if commit and user is not None:
            if not user.companies.filter(pk=tenant.pk).exists():
                user.companies.add(tenant)
            access = dict(user.branch_access or {})
            if access.get(schema) != 'all':
                access[schema] = 'all'
                user.branch_access = access
                user.save(update_fields=['branch_access'])

        if commit and new_password:
            self.stdout.write(self.style.WARNING(f'  пароль {SANDBOX_USERNAME}: {new_password}  (показан один раз)'))
        self.stdout.write(self.style.SUCCESS('готово' if commit else 'план показан, без --commit ничего не записано'))
