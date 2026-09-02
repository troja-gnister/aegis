from django.apps import AppConfig


class RootsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "aegis_apps.roots"

    def ready(self) -> None:
        from django.db.models.signals import m2m_changed

        from .signals import connect_membership_signals

        connect_membership_signals(m2m_changed)
