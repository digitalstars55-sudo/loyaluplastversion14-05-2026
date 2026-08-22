from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .serializers import (
    BirthdayProductSerializer,
    BirthdayPrizeClaimSerializer,
    BirthdayStatusSerializer,
    InventoryActivateSerializer,
    InventoryCooldownSerializer,
    InventoryItemSerializer,
    InventoryRequestSerializer,
    SuperPrizeClaimSerializer,
    SuperPrizeEntrySerializer,
)
from .services import (
    AlreadyActivated, AlreadyClaimed, BirthdayTooRecent,
    ClientNotFound, GiftClaimExpired, InvalidCode, InventoryCooldownActive,
    InventoryItemNotFound, NotBirthdayWindow, ProductNotFound, SuperPrizeNotFound,
    activate_inventory_cooldown, activate_item,
    claim_birthday_prize, claim_super_prize,
    get_birthday_products, get_birthday_status,
    get_inventory, get_inventory_cooldown, get_super_prizes,
)


class InventoryView(APIView):
    """
    GET /api/v1/inventory/?vk_id=&branch_id=
    Returns all InventoryItems for the guest, newest first.
    """

    @extend_schema(parameters=[InventoryRequestSerializer], responses={200: InventoryItemSerializer(many=True), 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = InventoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            items = get_inventory(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(InventoryItemSerializer(items, many=True, context={'request': request}).data)


class SuperPrizeView(APIView):
    """
    GET  /api/v1/super-prize/ — list all SuperPrizeEntries for the guest
    POST /api/v1/super-prize/ — guest selects a product from a pending entry
    """

    @extend_schema(parameters=[InventoryRequestSerializer], responses={200: SuperPrizeEntrySerializer(many=True), 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = InventoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            entries = get_super_prizes(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(SuperPrizeEntrySerializer(entries, many=True, context={'request': request}).data)

    @extend_schema(request=SuperPrizeClaimSerializer, responses={200: SuperPrizeEntrySerializer, 404: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = SuperPrizeClaimSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            entry = claim_super_prize(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except SuperPrizeNotFound:
            return Response(
                {'detail': 'Нет доступных суперпризов для выбора.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except ProductNotFound:
            return Response(
                {'detail': 'Товар не найден или недоступен как суперприз.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(SuperPrizeEntrySerializer(entry, context={'request': request}).data)


class InventoryCooldownView(APIView):
    """
    GET  /api/v1/inventory/cooldown/ — current INVENTORY cooldown status
    POST /api/v1/inventory/cooldown/ — manually activate cooldown (admin / debug)
    """

    @extend_schema(parameters=[InventoryRequestSerializer], responses={200: InventoryCooldownSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = InventoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            cooldown = get_inventory_cooldown(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if cooldown is None:
            return Response({'is_active': False, 'expires_at': None, 'seconds_remaining': 0})
        return Response(InventoryCooldownSerializer(cooldown).data)

    @extend_schema(request=InventoryRequestSerializer, responses={200: InventoryCooldownSerializer, 404: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = InventoryRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            cooldown = activate_inventory_cooldown(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(InventoryCooldownSerializer(cooldown).data)


class InventoryActivateView(APIView):
    """
    POST /api/v1/inventory/activate/

    Activates a pending InventoryItem so the guest can present it to staff.

    Birthday items  → require today's birthday code (DailyCode BIRTHDAY); no cooldown.
    All other items → check and set the 18-hour INVENTORY cooldown.
    """

    @extend_schema(request=InventoryActivateSerializer, responses={200: InventoryItemSerializer, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = InventoryActivateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            item = activate_item(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except InventoryItemNotFound:
            return Response(
                {'detail': 'Предмет инвентаря не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except GiftClaimExpired:
            return Response(
                {'detail': 'Срок подарка истёк — он сгорел.', 'reason': 'claim_expired'},
                status=status.HTTP_409_CONFLICT,
            )
        except AlreadyActivated:
            return Response(
                {'detail': 'Предмет уже активирован или использован.'},
                status=status.HTTP_409_CONFLICT,
            )
        except InvalidCode:
            return Response(
                {'detail': 'Неверный или не указанный код дня.', 'reason': 'bad_code'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InventoryCooldownActive as e:
            remaining = e.cooldown.remaining
            return Response(
                {
                    'detail': 'Инвентарь на перезарядке.',
                    'expires_at': e.cooldown.expires_at,
                    'seconds_remaining': int(remaining.total_seconds()) if remaining else 0,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(InventoryItemSerializer(item, context={'request': request}).data)


class BirthdayStatusView(APIView):
    """
    GET /api/v1/birthday/status/?vk_id=&branch_id=

    Returns birthday window status.  The frontend uses:
      is_birthday_window → show "Happy Birthday!" banner
      can_claim          → show the prize claim button
      already_claimed    → show "prize already claimed" message
    """

    @extend_schema(parameters=[InventoryRequestSerializer], responses={200: BirthdayStatusSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = InventoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            result = get_birthday_status(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(BirthdayStatusSerializer(result).data)


class BirthdayPrizeView(APIView):
    """
    GET  /api/v1/birthday/prize/?vk_id=&branch_id=
         Returns the pool of birthday products (is_birthday_prize=True).
         Only available within ±5 days of the guest's birthday and if not
         already claimed this year.

    POST /api/v1/birthday/prize/
         Reserves a birthday product as a pending InventoryItem.
         The guest activates it in the café via /inventory/activate/ using
         the birthday daily code.
    """

    @extend_schema(parameters=[InventoryRequestSerializer], responses={200: BirthdayProductSerializer(many=True), 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        s = InventoryRequestSerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        try:
            products = get_birthday_products(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except (NotBirthdayWindow, BirthdayTooRecent):
            return Response(
                {'detail': 'Не в периоде дня рождения.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except AlreadyClaimed:
            return Response(
                {'detail': 'Подарок на день рождения уже получен в этом году.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(BirthdayProductSerializer(products, many=True, context={'request': request}).data)

    @extend_schema(request=BirthdayPrizeClaimSerializer, responses={201: InventoryItemSerializer, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = BirthdayPrizeClaimSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            item = claim_birthday_prize(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except (NotBirthdayWindow, BirthdayTooRecent):
            return Response(
                {'detail': 'Не в периоде дня рождения.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except AlreadyClaimed:
            return Response(
                {'detail': 'Подарок на день рождения уже получен в этом году.'},
                status=status.HTTP_409_CONFLICT,
            )
        except ProductNotFound:
            return Response(
                {'detail': 'Товар не найден или недоступен как подарок на ДР.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(InventoryItemSerializer(item, context={'request': request}).data, status=status.HTTP_201_CREATED)
