"""
Карточка точки и её правка для внешнего кабинета (контракт платформы, ручка №15).

  GET   /api/v1/mobile/branches/{id}/   — полная карточка: контакты, адрес, ссылки на
                                           карты и отзывы, тексты подсказок гостю
  PATCH /api/v1/mobile/branches/{id}/   — правка тех же полей (частичная)

Что нарочно нельзя править отсюда: `branch_id` (публичный номер в QR-ссылках —
сменить его значит сломать напечатанные QR), `is_active` (скрывает точку от
гостей — это оператор платформы), интеграции с кассой. Право правки — только
администратор сети или суперпользователь: роль client в LoyalUP — «аналитика и
ответы на отзывы», точки она не редактирует и в веб-кабинете.

Поля живут в двух моделях (Branch и BranchConfig), снаружи это одна карточка —
разводим по моделям здесь, чтобы CheckUp об этом не знал.
"""
from __future__ import annotations

import logging

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, user_allowed_branches
from apps.tenant.branch.models import Branch, BranchConfig

log = logging.getLogger(__name__)

BRANCH_FIELDS = ('name', 'description', 'review_link_yandex', 'review_link_2gis', 'review_links_default')
CONFIG_FIELDS = ('address', 'phone', 'yandex_map', 'gis_map', 'code_prompt_message', 'quest_show_message',
                 'story_cafe_address', 'story_activation_text', 'story_saved_text')
READ_ONLY = ('id', 'branch_id', 'is_active')


class _BranchPatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = BRANCH_FIELDS


class _ConfigPatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchConfig
        fields = CONFIG_FIELDS


def branch_card(branch: Branch) -> dict:
    cfg = getattr(branch, 'config', None)
    card = {'id': branch.pk, 'branch_id': branch.branch_id, 'is_active': branch.is_active}
    for f in BRANCH_FIELDS:
        card[f] = getattr(branch, f)
    for f in CONFIG_FIELDS:
        card[f] = getattr(cfg, f, '') if cfg is not None else ''
    return card


def split_payload(data) -> tuple[dict, dict, list[str], list[str]]:
    """Тело → (поля Branch, поля BranchConfig, только-чтение, неизвестные). Чистая функция."""
    if not isinstance(data, dict):
        return {}, {}, [], ['<body>']
    branch_part, config_part, read_only, unknown = {}, {}, [], []
    for key, value in data.items():
        if key in BRANCH_FIELDS:
            branch_part[key] = value
        elif key in CONFIG_FIELDS:
            config_part[key] = value
        elif key in READ_ONLY:
            read_only.append(key)
        else:
            unknown.append(key)
    return branch_part, config_part, read_only, unknown


def can_edit_branches(user) -> bool:
    return bool(getattr(user, 'is_superuser', False) or getattr(user, 'role', '') == 'network_admin')


class BranchDetailAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_branch(self, request, pk: int):
        allowed = user_allowed_branches(request.user, current_schema_name())
        qs = Branch.objects.select_related('config')
        if allowed is not None:
            qs = qs.filter(pk__in=allowed)
        return qs.filter(pk=pk).first()

    def get(self, request, pk: int):
        branch = self._get_branch(request, pk)
        if branch is None:
            return Response({'code': 'not_found', 'detail': 'Точки нет или нет доступа'}, status=status.HTTP_404_NOT_FOUND)
        return Response(branch_card(branch))

    def patch(self, request, pk: int):
        if not can_edit_branches(request.user):
            return Response({'code': 'forbidden', 'detail': 'Точки правит администратор сети'}, status=status.HTTP_403_FORBIDDEN)
        branch_part, config_part, read_only, unknown = split_payload(request.data)
        if unknown or read_only:
            return Response({'code': 'invalid_payload',
                             'detail': 'Поля только для чтения или неизвестные',
                             'read_only': read_only, 'unknown': unknown,
                             'editable': list(BRANCH_FIELDS + CONFIG_FIELDS)}, status=status.HTTP_400_BAD_REQUEST)
        if not branch_part and not config_part:
            return Response({'code': 'invalid_payload', 'detail': 'Нечего менять'}, status=status.HTTP_400_BAD_REQUEST)

        branch = self._get_branch(request, pk)
        if branch is None:
            return Response({'code': 'not_found', 'detail': 'Точки нет или нет доступа'}, status=status.HTTP_404_NOT_FOUND)

        b_ser = _BranchPatchSerializer(branch, data=branch_part, partial=True)
        cfg, _ = BranchConfig.objects.get_or_create(branch=branch)
        c_ser = _ConfigPatchSerializer(cfg, data=config_part, partial=True)
        errors = {}
        if not b_ser.is_valid():
            errors.update(b_ser.errors)
        if not c_ser.is_valid():
            errors.update(c_ser.errors)
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        changed = sorted(list(branch_part) + list(config_part))
        if branch_part:
            b_ser.save()
        if config_part:
            c_ser.save()

        try:
            from apps.shared.audit.services import record_event
            record_event(action='update', request=request, actor=request.user,
                         target=f'Точка «{branch.name}» (id={branch.pk})',
                         meta={'via': 'api', 'fields': changed})
        except Exception:
            pass
        log.info('branch patch: %s id=%s fields=%s by=%s', current_schema_name(), branch.pk, changed,
                 getattr(request.user, 'username', '?'))

        branch = Branch.objects.select_related('config').get(pk=branch.pk)
        return Response({**branch_card(branch), 'changed': changed})
