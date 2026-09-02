import pytest
from aegis_apps.roots.permissions import (
    Permission,
    permission_names,
    validate_permission_mask,
)


def test_permission_values_are_stable_independent_bits() -> None:
    assert list(Permission) == [
        Permission.BROWSE,
        Permission.PREVIEW,
        Permission.EXPORT,
        Permission.CREATE,
        Permission.ORGANIZE,
        Permission.COPY,
        Permission.DELETE_RESTORE,
        Permission.ROOT_ADMIN,
    ]
    assert [int(permission) for permission in Permission] == [1, 2, 4, 8, 16, 32, 64, 128]
    assert Permission.BROWSE | Permission.PREVIEW == 3


@pytest.mark.parametrize("value", [False, True, -1, 256, 512, "1", None])
def test_permission_mask_rejects_non_integer_out_of_range_or_unknown_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="permission mask"):
        validate_permission_mask(value)


def test_permission_mask_accepts_zero_and_emits_only_canonical_names() -> None:
    assert validate_permission_mask(0) == Permission(0)
    mask = validate_permission_mask(255)

    assert permission_names(mask) == [
        "browse",
        "preview",
        "export",
        "create",
        "organize",
        "copy",
        "delete_restore",
        "root_admin",
    ]
