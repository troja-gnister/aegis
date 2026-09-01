import pytest
from aegis_apps.identity.models import GroupIdentity, User
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

pytestmark = pytest.mark.integration


@pytest.mark.django_db(transaction=True)
def test_database_rejects_empty_username() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create(username="")

    assert User.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_group_identity_protects_group_from_direct_deletion() -> None:
    group = Group.objects.create(name="Protected Group")
    identity = GroupIdentity.objects.create(group=group)

    with pytest.raises(ProtectedError):
        group.delete()

    assert Group.objects.filter(pk=group.pk).exists()
    assert GroupIdentity.objects.filter(pk=identity.pk).exists()
