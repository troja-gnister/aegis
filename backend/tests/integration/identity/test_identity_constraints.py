import pytest
from aegis_apps.identity.models import User
from django.db import IntegrityError, transaction

pytestmark = pytest.mark.integration


@pytest.mark.django_db(transaction=True)
def test_database_rejects_empty_username() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create(username="")

    assert User.objects.count() == 0
