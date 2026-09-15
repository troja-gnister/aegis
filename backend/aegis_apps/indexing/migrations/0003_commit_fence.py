from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("indexing", "0002_directory_counter_guard"),
        ("catalog", "0002_source_parent_revision"),
    ]

    operations = [
        migrations.AddField(
            model_name="directorywork", name="last_batch_hash",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddIndex(
            model_name="scanrequest",
            index=models.Index(fields=("actor", "root", "created_at"),
                               name="indexing_request_recent_idx"),
        ),
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION public.aegis_guard_directory_commit()
                RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $guard$
                BEGIN
                    -- Migration/admin fixture maintenance is not indexer authority.
                    -- Pending child insertion and expired-attempt bookkeeping publish no observations.
                    IF session_user <> 'aegis_indexer' OR NEW.lease_owner IS NULL THEN
                        RETURN NEW;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM public.indexing_directorywork AS work
                        JOIN public.indexing_scanrun AS run ON run.id=work.run_id
                        JOIN public.roots_root AS root ON root.id=run.root_id
                        JOIN public.indexing_rootindexstate AS state ON state.root_id=root.id
                        JOIN public.indexing_indexdeployment AS deployment ON deployment.id=1
                        JOIN public.catalog_catalogentry AS directory ON directory.id=work.directory_id
                        WHERE work.id=NEW.id AND work.attempt=NEW.attempt
                          AND work.lease_owner=NEW.lease_owner AND work.parent_revision=NEW.parent_revision
                          AND work.lease_expires_at>pg_catalog.clock_timestamp()
                          AND work.lease_expires_at<=pg_catalog.clock_timestamp()+interval '60 seconds'
                          AND run.state='running' AND state.active_run_id=run.id AND root.active
                          AND deployment.slot_ids ? root.slot_id
                          AND run.binding_epoch=deployment.epoch AND run.policy_epoch=deployment.epoch
                          AND state.binding_epoch=deployment.epoch AND state.policy_epoch=deployment.epoch
                          AND run.root_epoch=root.authorization_epoch
                          AND run.start_epoch=state.reconciliation_epoch
                          AND run.manifest_identity=deployment.manifest_identity
                          AND directory.root_id=root.id AND directory.kind='directory'
                          AND directory.source_revision=work.parent_revision
                    ) THEN
                        RAISE EXCEPTION 'scan commit fence rejected' USING ERRCODE='55000';
                    END IF;
                    RETURN NEW;
                END;
                $guard$;
                REVOKE ALL ON FUNCTION public.aegis_guard_directory_commit() FROM PUBLIC;
                CREATE CONSTRAINT TRIGGER aegis_guard_directory_commit
                    AFTER INSERT OR UPDATE ON public.indexing_directorywork
                    DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION public.aegis_guard_directory_commit();
            """,
            reverse_sql="""
                DROP TRIGGER aegis_guard_directory_commit ON public.indexing_directorywork;
                DROP FUNCTION public.aegis_guard_directory_commit();
            """,
        ),
    ]
