from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "aegis_apps.identity"

    def ready(self) -> None:
        from django.contrib.auth.signals import user_logged_in

        from .session_policy import initialize_logged_in_session

        user_logged_in.connect(
            initialize_logged_in_session,
            dispatch_uid="aegis.identity.initialize_logged_in_session",
            weak=False,
        )
