from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="auditevent",
            options={
                "base_manager_name": "objects",
                "default_manager_name": "objects",
                "ordering": ("-occurred_at", "-id"),
            },
        ),
    ]
