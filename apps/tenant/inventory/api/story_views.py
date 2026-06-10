from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .story_serializers import (
    StoryAccessSerializer,
    StoryActivateSerializer,
    StoryGiftSerializer,
    StoryProductSerializer,
    StoryRequestSerializer,
    StorySelectSerializer,
)
from . import story_services as svc


def _not_found(detail='Профиль гостя не найден.'):
    return Response({'detail': detail}, status=status.HTTP_404_NOT_FOUND)


class StoryAccessView(APIView):
    """
    GET /api/v1/story/access/?vk_id=&branch_id=
    Состояние доступа к игре через сториз (включено / можно играть / уже играл).
    """

    @extend_schema(parameters=[StoryRequestSerializer], responses={200: StoryAccessSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = StoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            data = svc.get_story_access(**s.validated_data)
        except svc.ClientNotFound:
            return _not_found()
        return Response(StoryAccessSerializer(data).data)


class StoryPlayView(APIView):
    """
    POST /api/v1/story/play/
    Сыграть в игру через сториз (одноразово на VK ID на точку). Всегда «выигрыш» —
    далее пользователь выбирает подарок через /story/gifts + /story/select.
    """

    @extend_schema(request=StoryRequestSerializer, responses={200: StoryAccessSerializer, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = StoryRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            svc.play_story_game(**s.validated_data)
        except svc.ClientNotFound:
            return _not_found()
        except svc.StoryDisabled:
            return Response({'detail': 'Игра через сториз сейчас недоступна.'}, status=status.HTTP_403_FORBIDDEN)
        except svc.NoStoryGifts:
            return Response({'detail': 'Подарки для сториз не настроены.'}, status=status.HTTP_409_CONFLICT)
        except svc.StoryAlreadyPlayed:
            return Response(
                {'detail': 'Вы уже получали подарок через сториз. Найдите его в разделе «Мои подарки».'},
                status=status.HTTP_409_CONFLICT,
            )
        # Возвращаем обновлённое состояние доступа (теперь already_played=true, статус played)
        data = svc.get_story_access(**s.validated_data)
        return Response(StoryAccessSerializer(data).data)


class StoryGiftsView(APIView):
    """
    GET /api/v1/story/gifts/?vk_id=&branch_id=
    Пул подарков для сториз (экран выбора). Доступен после игры.
    """

    @extend_schema(parameters=[StoryRequestSerializer], responses={200: StoryProductSerializer(many=True), 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = StoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            products = svc.get_story_gifts(**s.validated_data)
        except svc.ClientNotFound:
            return _not_found()
        except svc.StoryNotPlayed:
            return Response({'detail': 'Сначала сыграйте в игру.'}, status=status.HTTP_403_FORBIDDEN)
        except svc.NoStoryGifts:
            return Response({'detail': 'Подарки для сториз не настроены.'}, status=status.HTTP_409_CONFLICT)
        return Response(StoryProductSerializer(products, many=True, context={'request': request}).data)


class StorySelectView(APIView):
    """
    POST /api/v1/story/select/
    Выбрать подарок из набора сториз → сохранить в «Мои подарки».
    Фиксирует метрику «Получили подарок через сториз».
    """

    @extend_schema(request=StorySelectSerializer, responses={200: StoryGiftSerializer, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = StorySelectSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            entry = svc.select_story_gift(**s.validated_data)
        except svc.ClientNotFound:
            return _not_found()
        except svc.StoryNotPlayed:
            return Response({'detail': 'Сначала сыграйте в игру.'}, status=status.HTTP_409_CONFLICT)
        except svc.StoryAlreadySelected:
            return Response({'detail': 'Подарок уже выбран.'}, status=status.HTTP_409_CONFLICT)
        except svc.ProductNotFound:
            return Response({'detail': 'Подарок недоступен.'}, status=status.HTTP_404_NOT_FOUND)
        settings = svc.get_story_settings(s.validated_data['vk_id'], s.validated_data['branch_id'])
        return Response(StoryGiftSerializer(entry, context={'request': request, 'settings': settings}).data)


class StoryGiftView(APIView):
    """
    GET /api/v1/story/gift/?vk_id=&branch_id=
    Текущий story-подарок пользователя для «Мои подарки» (или {} если нет).
    """

    @extend_schema(parameters=[StoryRequestSerializer], responses={200: StoryGiftSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = StoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            entry = svc.get_story_gift(**s.validated_data)
        except svc.ClientNotFound:
            return _not_found()
        if entry is None:
            return Response({})
        settings = svc.get_story_settings(**s.validated_data)
        return Response(StoryGiftSerializer(entry, context={'request': request, 'settings': settings}).data)


class StoryActivateView(APIView):
    """
    POST /api/v1/story/activate/
    Активация подарка из сториз в кафе.

    Требует код дня (DailyCodePurpose.GAME). Без валидного кода — 409 с текстом
    инструкции (instruction_text), таймер НЕ запускается. С валидным кодом —
    200 с активированным подарком (метрика «Активировали через сториз»).
    """

    @extend_schema(request=StoryActivateSerializer, responses={200: StoryGiftSerializer, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = StoryActivateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            entry = svc.activate_story_gift(**s.validated_data)
        except svc.ClientNotFound:
            return _not_found()
        except svc.StoryGiftNotFound:
            return Response({'detail': 'Подарок недоступен для активации.'}, status=status.HTTP_404_NOT_FOUND)
        except svc.StoryAlreadyActivated:
            return Response({'detail': 'Подарок уже активирован.'}, status=status.HTTP_409_CONFLICT)
        except svc.StoryActivationDenied as denied:
            return Response(
                {
                    'detail': 'Подарок активируется только в кафе по коду дня.',
                    'reason': denied.reason,            # need_code | bad_code
                    'instruction_text': denied.instruction_text,
                    'activated': False,
                },
                status=status.HTTP_409_CONFLICT,
            )
        settings = svc.get_story_settings(s.validated_data['vk_id'], s.validated_data['branch_id'])
        return Response(StoryGiftSerializer(entry, context={'request': request, 'settings': settings}).data)
