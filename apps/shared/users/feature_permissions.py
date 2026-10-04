"""Feature check for employee APIs, in addition to their existing role/tenant gates."""
from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.shared.users.access import user_can_feature


class HasFeatureAccess(BasePermission):
    message = 'Нет доступа к этому разделу.'
    required_feature = None
    write_feature = None

    def has_permission(self, request, view):
        if not user_can_feature(request.user, self.required_feature):
            return False
        if self.write_feature and request.method not in SAFE_METHODS:
            return user_can_feature(request.user, self.write_feature)
        return True

    @classmethod
    def factory(cls, feature, write_feature=None):
        return type('FeatureAccess_' + feature, (cls,), {
            'required_feature': feature, 'write_feature': write_feature,
        })
