from enum import IntFlag


class Permission(IntFlag):
    BROWSE = 1
    PREVIEW = 2
    EXPORT = 4
    CREATE = 8
    ORGANIZE = 16
    COPY = 32
    DELETE_RESTORE = 64
    ROOT_ADMIN = 128


_ALL_PERMISSION_BITS = sum(int(permission) for permission in Permission)
_CANONICAL_NAMES = (
    (Permission.BROWSE, "browse"),
    (Permission.PREVIEW, "preview"),
    (Permission.EXPORT, "export"),
    (Permission.CREATE, "create"),
    (Permission.ORGANIZE, "organize"),
    (Permission.COPY, "copy"),
    (Permission.DELETE_RESTORE, "delete_restore"),
    (Permission.ROOT_ADMIN, "root_admin"),
)


def validate_permission_mask(value: object) -> Permission:
    if isinstance(value, bool):
        raise ValueError("invalid permission mask")
    if isinstance(value, Permission):
        mask = int(value)
    elif type(value) is int:
        mask = value
    else:
        raise ValueError("invalid permission mask")
    if mask < 0 or mask > _ALL_PERMISSION_BITS or mask & ~_ALL_PERMISSION_BITS:
        raise ValueError("invalid permission mask")
    return Permission(mask)


def permission_names(value: object) -> list[str]:
    mask = validate_permission_mask(value)
    return [name for permission, name in _CANONICAL_NAMES if mask & permission]
