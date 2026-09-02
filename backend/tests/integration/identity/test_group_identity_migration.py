from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.integration


@pytest.mark.django_db(transaction=True)
def test_group_identity_migration_backfills_existing_groups_with_random_uuids() -> None:
    executor = MigrationExecutor(connection)
    current_leaf_nodes = executor.loader.graph.leaf_nodes()
    before = [("identity", "0002_identity_constraints")]
    after = [("identity", "0003_group_identity")]
    try:
        executor.migrate(before)
        old_apps = executor.loader.project_state(before).apps
        group_model = old_apps.get_model("auth", "Group")
        first = group_model.objects.create(name="Existing One")
        second = group_model.objects.create(name="Existing Two")

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new_apps = executor.loader.project_state(after).apps
        identity_model = new_apps.get_model("identity", "GroupIdentity")
        identities = list(
            identity_model.objects.filter(group_id__in=[first.pk, second.pk]).order_by(
                "group_id"
            )
        )

        assert len(identities) == 2
        assert all(
            isinstance(identity.id, uuid.UUID) and identity.id.version == 4
            for identity in identities
        )
        assert identities[0].id != identities[1].id
    finally:
        MigrationExecutor(connection).migrate(current_leaf_nodes)
