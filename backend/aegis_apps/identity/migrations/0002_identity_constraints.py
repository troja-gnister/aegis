from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=~models.Q(username=""),
                name="identity_user_username_nonempty",
            ),
        ),
    ]
