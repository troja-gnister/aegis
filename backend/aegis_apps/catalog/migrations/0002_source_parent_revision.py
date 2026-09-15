from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="catalogentry",
            name="source_parent_revision",
            field=models.PositiveBigIntegerField(null=True),
        ),
    ]
