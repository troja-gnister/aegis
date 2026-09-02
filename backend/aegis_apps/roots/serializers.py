from __future__ import annotations

from typing import Any, Protocol, cast

from rest_framework import serializers

from .models import Root
from .permissions import permission_names


class _AuthorizedRoot(Protocol):
    effective_permissions: int


class RootShellSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(read_only=True)
    displayName = serializers.CharField(source="display_name", read_only=True)
    mode = serializers.ChoiceField(choices=Root.Mode.values, read_only=True)
    permissions = serializers.SerializerMethodField()
    authorizationEpoch = serializers.IntegerField(source="authorization_epoch", read_only=True)

    def get_permissions(self, obj: Root) -> list[str]:
        row = cast(_AuthorizedRoot, obj)
        return permission_names(row.effective_permissions)
