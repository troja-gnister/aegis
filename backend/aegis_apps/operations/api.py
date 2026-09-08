from __future__ import annotations

from typing import Any

from rest_framework.authentication import SessionAuthentication
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from aegis_apps.identity.api import AUTHENTICATION_REQUIRED
from aegis_apps.identity.models import User
from aegis_apps.roots.manifest import ManifestError

from .selectors import SchemaCompatibilityError, operations_status
from .serializers import OperationRoleStatusSerializer

OPERATIONS_STATUS_UNAVAILABLE = {
    "type": "operations_status_unavailable",
    "title": "Operations status unavailable",
}


def _response(data: Any = None, *, status: int = 200) -> Response:
    response = Response(data, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


class OperationsStatusView(APIView):
    authentication_classes = (SessionAuthentication,)
    permission_classes = ()
    renderer_classes = (JSONRenderer,)

    def finalize_response(
        self,
        request: Request,
        response: Response,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        finalized = super().finalize_response(request, response, *args, **kwargs)
        finalized["Cache-Control"] = "private, no-store"
        return finalized

    def get(self, request: Request) -> Response:
        user = request.user
        if not isinstance(user, User) or not user.is_authenticated:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        if not user.is_staff:
            return _response(status=404)
        try:
            role_status = operations_status()
        except (ManifestError, SchemaCompatibilityError, ValueError):
            return _response(OPERATIONS_STATUS_UNAVAILABLE, status=503)
        serializer = OperationRoleStatusSerializer(role_status, many=True)
        return _response({"roles": serializer.data})
