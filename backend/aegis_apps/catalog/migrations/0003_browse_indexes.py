from django.db import migrations, models
from django.db.models.functions import Coalesce


def indexes() -> list[models.Index]:
    result = []
    for sort, field in (("name", "name_key"), ("modified", "mtime_ns"), ("size", "size")):
        for descending in (False, True):
            values: list[models.F | models.Func] = [models.F("name_key"), models.F("id")]
            ranks = [models.Case(models.When(kind="directory", then=0), default=1)]
            if sort != "name":
                ranks.append(models.Case(models.When(**{f"{field}__isnull": True}, then=1), default=0))
                values.insert(0, Coalesce(field, 0, output_field=models.DecimalField()))
            result.append(models.Index(
                models.F("root"), models.F("logical_parent"),
                models.Case(models.When(source_state="missing", then=True), default=False),
                *ranks, *(value.desc() if descending else value.asc() for value in values),
                name=f"catalog_browse_{sort}_{'desc' if descending else 'asc'}",
            ))
    return result


class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_source_parent_revision")]
    operations = [migrations.AddIndex(model_name="catalogentry", index=index) for index in indexes()]
