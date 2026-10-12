from django.db import migrations, models
from django.db.models.functions import Coalesce


def indexes() -> list[models.Index]:
    result = []
    for sort, field in (("name", "name_key"), ("modified", "mtime_ns"), ("size", "size")):
        for typed in (False, True):
            ranks: list[models.F | models.Func | models.Case] = [
                models.Case(models.When(source_state="missing", then=True), default=False),
                models.Case(models.When(kind="directory", then=0), default=1),
            ]
            if sort != "name":
                ranks.append(
                    models.Case(models.When(**{f"{field}__isnull": True}, then=1), default=0)
                )
                ranks.append(Coalesce(field, 0, output_field=models.DecimalField()))
            result.append(
                models.Index(
                    models.F("root"),
                    *((models.F("type_hint"),) if typed else ()),
                    *ranks,
                    models.F("name_key"),
                    models.F("id"),
                    name=f"catalog_root_{'type_' if typed else ''}{sort}",
                )
            )
    return result


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_browse_indexes")]
    operations = [
        migrations.AddIndex(model_name="catalogentry", index=index) for index in indexes()
    ]
