from django.contrib.admin.apps import AdminConfig


class AegisAdminConfig(AdminConfig):
    default_site = "aegis_apps.identity.admin_site.AegisAdminSite"
