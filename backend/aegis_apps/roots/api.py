from __future__ import annotations

from typing import Any

from rest_framework.authentication import SessionAuthentication
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from aegis_apps.identity.api import AUTHENTICATION_REQUIRED
from aegis_apps.identity.models import User

from .manifest import ManifestError, configured_manifest
from .selectors import authorized_roots
from .serializers import RootShellSerializer

ROOT_CATALOG_UNAVAILABLE = {
    "type": "root_catalog_unavailable",
    "title": "Root catalog unavailable",
}


def _response(data: Any = None, *, status: int = 200) -> Response:
    response = Response(data, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


class RootListView(APIView):
    authentication_classes = (SessionAuthentication,)
    permission_classes = ()
    renderer_classes = (JSONRenderer,)

    def get(self, request: Request) -> Response:
        user = request.user
        if not isinstance(user, User) or not user.is_authenticated:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        try:
            manifest = configured_manifest()
        except ManifestError:
            return _response(ROOT_CATALOG_UNAVAILABLE, status=503)
        if manifest is None:
            return _response({"roots": []})
        roots = authorized_roots(
            user_id=user.id,
            active_manifest_slot_ids=tuple(manifest.slots),
        )
        serializer = RootShellSerializer(roots, many=True)
        return _response({"roots": serializer.data})
