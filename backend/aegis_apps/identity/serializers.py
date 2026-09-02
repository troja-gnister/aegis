from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from rest_framework import serializers


class StrictSerializer(serializers.Serializer[Any]):
    def to_internal_value(self, data: object) -> dict[str, Any]:
        if not isinstance(data, Mapping):
            raise serializers.ValidationError("Expected a JSON object.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError("Unknown fields are not allowed.")
        return cast(dict[str, Any], super().to_internal_value(data))


class StrictStringField(serializers.CharField):
    def __init__(self, *, max_bytes: int | None = None, **kwargs: Any) -> None:
        self.max_bytes = max_bytes
        super().__init__(**kwargs)

    def to_internal_value(self, data: object) -> str:
        if not isinstance(data, str):
            self.fail("invalid")
        if self.max_bytes is not None and len(data.encode("utf-8")) > self.max_bytes:
            self.fail("max_length", max_length=self.max_bytes)
        return super().to_internal_value(data)


class LoginSerializer(StrictSerializer):
    username = StrictStringField(
        max_length=150,
        allow_blank=False,
        trim_whitespace=False,
    )
    password = StrictStringField(
        max_length=1024,
        max_bytes=1024,
        allow_blank=False,
        trim_whitespace=False,
        write_only=True,
    )
