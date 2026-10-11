"""Media delivery routes, mounted at ``/media/v1/`` (gateway location ``/media/``)."""

from django.urls import path

from .delivery import DerivativeDeliveryView, OriginalDeliveryView

urlpatterns = [
    path(
        "derivatives/<uuid:entry_id>/<str:kind>",
        DerivativeDeliveryView.as_view(),
        name="media-derivative",
    ),
    path(
        "originals/<uuid:entry_id>",
        OriginalDeliveryView.as_view(),
        name="media-original",
    ),
]
