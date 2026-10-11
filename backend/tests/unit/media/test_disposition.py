from __future__ import annotations

from urllib.parse import unquote

import pytest
from aegis_apps.media.disposition import content_disposition, display_filename


@pytest.mark.parametrize(
    ("disposition", "raw", "expected"),
    [
        (
            "attachment",
            b"IMG_0001.JPG",
            "attachment; filename=\"IMG_0001.JPG\"; filename*=UTF-8''IMG_0001.JPG",
        ),
        (
            "inline",
            b"space name.jpg",
            "inline; filename=\"space name.jpg\"; filename*=UTF-8''space%20name.jpg",
        ),
        (
            "attachment",
            "caf\u00e9 \u6771\u4eac.jpg".encode(),
            "attachment; filename=\"caf_ __.jpg\"; "
            "filename*=UTF-8''caf%C3%A9%20%E6%9D%B1%E4%BA%AC.jpg",
        ),
        (
            "attachment",
            b"100% \"quoted\" back\\slash.jpg",
            "attachment; filename=\"100_ _quoted_ back_slash.jpg\"; "
            "filename*=UTF-8''100%25%20_quoted_%20back_slash.jpg",
        ),
        (
            "attachment",
            b"latin\xe9.jpg",
            "attachment; filename=\"latin_.jpg\"; filename*=UTF-8''latin%EF%BF%BD.jpg",
        ),
        (
            "attachment",
            b"evil\r\nSet-Cookie: x=1.jpg",
            "attachment; filename=\"evil__Set-Cookie: x=1.jpg\"; "
            "filename*=UTF-8''evil__Set-Cookie%3A%20x%3D1.jpg",
        ),
        (
            "attachment",
            b"semi;colon,comma.jpg",
            "attachment; filename=\"semi;colon,comma.jpg\"; "
            "filename*=UTF-8''semi%3Bcolon%2Ccomma.jpg",
        ),
        (
            "attachment",
            "rtl\u202egpj.exe".encode(),
            "attachment; filename=\"rtl_gpj.exe\"; filename*=UTF-8''rtl_gpj.exe",
        ),
        (
            "attachment",
            "line\u2028break.jpg".encode(),
            "attachment; filename=\"line_break.jpg\"; filename*=UTF-8''line_break.jpg",
        ),
    ],
)
def test_content_disposition_vectors(disposition: str, raw: bytes, expected: str) -> None:
    value = content_disposition(disposition, raw)  # type: ignore[arg-type]

    assert value == expected
    assert value.isascii()
    assert "\r" not in value and "\n" not in value


def test_rfc8187_value_decodes_to_the_safe_display_name() -> None:
    raw = "\u00e9t\u00e9 \U0001f600 #1.jpg".encode()
    value = content_disposition("attachment", raw)
    encoded = value.split("filename*=UTF-8''", 1)[1]

    assert unquote(encoded, encoding="utf-8", errors="strict") == display_filename(raw)
    assert display_filename(raw) == "\u00e9t\u00e9 \U0001f600 #1.jpg"


@pytest.mark.parametrize("raw", [b"", b" ", b"\x01\x02", b"..", b"."])
def test_empty_or_unusable_names_fall_back_safely(raw: bytes) -> None:
    value = content_disposition("attachment", raw)

    assert 'filename="' in value
    assert "filename*=UTF-8''" in value
    assert '"."' not in value and '".."' not in value


def test_only_inline_and_attachment_are_accepted() -> None:
    with pytest.raises(ValueError, match="disposition"):
        content_disposition("form-data", b"a.jpg")  # type: ignore[arg-type]
