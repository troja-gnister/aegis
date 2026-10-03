from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "aegis_apps.common"

    def ready(self) -> None:
        from django.conf import settings

        if getattr(settings, "AEGIS_WEB_DATABASE_POOL", None) is not None:
            from .database_pool import install

            install()
