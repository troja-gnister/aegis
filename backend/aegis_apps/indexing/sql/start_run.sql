INSERT INTO public.catalog_catalogentry (
    id, root_id, source_parent_id, logical_parent_id, raw_name, display_name, name_key,
    logical_name, kind, type_hint, source_state, size, mtime_ns, ctime_ns, device, inode,
    source_revision, catalog_version, children_version, observation_epoch,
    seen_generation, seen_attempt, observed_at
) VALUES (pg_catalog.gen_random_uuid(), p_root_id, NULL, NULL, ''::bytea, '', ''::bytea,
    NULL, 'directory', NULL, 'present', NULL, NULL, NULL, NULL, NULL, 0, 0, 0, 0, 0, 0, NULL)
ON CONFLICT (root_id) WHERE source_parent_id IS NULL DO NOTHING;
SELECT id, source_revision INTO anchor_id, anchor_revision FROM public.catalog_catalogentry
    WHERE root_id=p_root_id AND source_parent_id IS NULL;
INSERT INTO public.indexing_scanrun (
    id, root_id, binding_epoch, policy_epoch, root_epoch, manifest_identity,
    generation, start_epoch, state, started_at, settled_at
) VALUES (pg_catalog.gen_random_uuid(), p_root_id, deployment.epoch, deployment.epoch,
    root.authorization_epoch, deployment.manifest_identity, root_state.next_generation,
    root_state.reconciliation_epoch, 'queued', database_now, NULL) RETURNING * INTO run;
INSERT INTO public.indexing_directorywork (
    id, run_id, directory_id, parent_revision, state, attempt, lease_owner, lease_expires_at,
    available_at, last_batch_sequence, observed_count, eof_identity, error_code, updated_at
) VALUES (pg_catalog.gen_random_uuid(), run.id, anchor_id, anchor_revision, 'pending', 0,
    NULL, NULL, database_now, 0, 0, NULL, NULL, database_now);
