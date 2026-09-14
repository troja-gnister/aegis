from uuid import UUID

from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.identity.models import User

from .database import ScanRequestThrottled as ScanRequestThrottled
from .database import _call


def request_root_scan(actor: User, root_id: UUID, request_id: str) -> UUID:
    if (
        not isinstance(actor, User)
        or not isinstance(actor.pk, UUID)
        or not isinstance(root_id, UUID)
        or not isinstance(request_id, str)
        or REQUEST_ID.fullmatch(request_id) is None
        or type(actor.authorization_epoch) is not int
        or not 0 <= actor.authorization_epoch <= 9223372036854775807
    ):
        raise ValueError("invalid scan request")
    result = _call("request", [root_id, actor.pk, actor.authorization_epoch, request_id])
    return UUID(str(result))
