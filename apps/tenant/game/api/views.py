from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .serializers import (
    GameClaimSerializer,
    GameCooldownRequestSerializer,
    GameCooldownSerializer,
    GameSessionSerializer,
    GameStartSerializer,
    SuperPrizeRewardSerializer,
)
from .services import (
    ClientNotFound, CodeRequired, DeliveryCodeNotActivated,
    GameCooldownActive, InvalidCode, InvalidToken, VKSubscriptionRequired,
    claim_game, get_game_cooldown, reset_game_cooldown, start_game,
)


class GameStartView(APIView):
    """
    POST /api/v1/game/start/

    Phase 1: validates the guest's eligibility and pre-determines the reward.
    Returns a signed session token and an animation `score` (1–10).

    The client drives the rocket animation to `score` height WITHOUT knowing
    the actual reward.  Once the animation completes, call /game/claim/ to
    reveal and record the reward.

    This two-phase design lets the animation play first, then surprise the
    guest with the result — rather than showing the outcome upfront.
    """

    @extend_schema(request=GameStartSerializer, responses={200: GameSessionSerializer, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        data = {**request.query_params.dict(), **request.data}
        s = GameStartSerializer(data=data)
        s.is_valid(raise_exception=True)

        try:
            result = start_game(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except GameCooldownActive as e:
            remaining = e.cooldown.remaining
            return Response(
                {
                    'detail': 'Игра на перезарядке.',
                    'expires_at': e.cooldown.expires_at,
                    'seconds_remaining': int(remaining.total_seconds()) if remaining else 0,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except CodeRequired as e:
            # score позволяет фронт-колесу докрутиться до сектора ДО ввода кода
            return Response({'needs_code': True, 'score': e.score})
        except InvalidCode:
            return Response(
                {'detail': 'Неверный код дня.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(GameSessionSerializer(result).data)


class GameClaimView(APIView):
    """
    POST /api/v1/game/claim/

    Phase 2: reveals the reward, records the game attempt, and activates
    the 18-hour cooldown.  Must be called after the animation completes.

    The session token expires in 10 minutes and is replay-safe: claiming
    the same token twice returns 400 on the second request.
    """

    @extend_schema(request=GameClaimSerializer, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        data = {**request.query_params.dict(), **request.data}
        s = GameClaimSerializer(data=data)
        s.is_valid(raise_exception=True)

        try:
            result = claim_game(**s.validated_data)
        except InvalidToken:
            return Response(
                {'detail': 'Сессия игры недействительна или истекла.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DeliveryCodeNotActivated:
            return Response(
                {'detail': 'Необходимо ввести код доставки.', 'needs_delivery_code': True},
                status=status.HTTP_403_FORBIDDEN,
            )
        except VKSubscriptionRequired as e:
            return Response(
                {
                    'detail': 'Подпишитесь на сообщество и рассылку, чтобы получить приз.',
                    'needs_vk_subscription': True,
                    'is_community_member': e.is_community_member,
                    'is_newsletter_subscriber': e.is_newsletter_subscriber,
                    'prize_preview': e.prize_preview,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if result['type'] == 'super_prize':
            return Response({
                'type': 'super_prize',
                'reward': SuperPrizeRewardSerializer(result['reward'], context={'request': request}).data,
            })

        return Response({'type': 'coin', 'reward': result['reward']})


class GameCooldownView(APIView):
    """
    GET    /api/v1/game/cooldown/ — current GAME cooldown status
    DELETE /api/v1/game/cooldown/ — reset cooldown (admin / debug)
    """

    @extend_schema(parameters=[GameCooldownRequestSerializer], responses={200: GameCooldownSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = GameCooldownRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)

        try:
            cooldown = get_game_cooldown(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if cooldown is None:
            return Response({'is_active': False, 'expires_at': None, 'seconds_remaining': 0})

        return Response(GameCooldownSerializer(cooldown).data)

    @extend_schema(parameters=[GameCooldownRequestSerializer], responses={204: None, 404: OpenApiTypes.OBJECT})
    def delete(self, request: Request) -> Response:
        s = GameCooldownRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)

        try:
            reset_game_cooldown(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)
