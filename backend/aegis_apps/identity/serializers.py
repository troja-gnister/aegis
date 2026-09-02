from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from typing import IO, Any, cast, override

from django.conf import settings
from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


class BoundedJSONParser(JSONParser):
    @override
    def parse(
        self,
        stream: IO[Any],
        media_type: str | None = None,
        parser_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = stream.read(settings.AEGIS_AUTH_REQUEST_BODY_MAX_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > settings.AEGIS_AUTH_REQUEST_BODY_MAX_BYTES:
            raise ParseError("Invalid request")
        return super().parse(
            BytesIO(raw),
            media_type=media_type,
            parser_context=parser_context,
        )


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
        try:
            encoded = data.encode("utf-8")
        except UnicodeEncodeError:
            self.fail("invalid")
        if self.max_bytes is not None and len(encoded) > self.max_bytes:
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
