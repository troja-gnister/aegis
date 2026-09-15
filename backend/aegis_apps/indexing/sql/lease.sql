CREATE OR REPLACE FUNCTION public.aegis_claim_scan_directory(p_run_id uuid, p_worker_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $claim$
DECLARE
    deployment public.indexing_indexdeployment%ROWTYPE;
    root public.roots_root%ROWTYPE;
    root_state public.indexing_rootindexstate%ROWTYPE;
    run public.indexing_scanrun%ROWTYPE;
    work public.indexing_directorywork%ROWTYPE;
    directory public.catalog_catalogentry%ROWTYPE;
    discovered_root uuid;
    database_now timestamptz;
    lease_seconds CONSTANT integer := 60;
    max_directory_attempts CONSTANT integer := 3;
    first_retry_seconds CONSTANT integer := 5;
    second_retry_seconds CONSTANT integer := 10;
BEGIN
    IF session_user <> 'aegis_indexer' THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    IF p_run_id IS NULL OR p_worker_id IS NULL
       OR p_worker_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'invalid scan request' USING ERRCODE = '22023';
    END IF;
    -- Bounded discovery is read-only; acquire authority before any work-row lock.
    SELECT root_id INTO discovered_root FROM public.indexing_scanrun WHERE id=p_run_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO deployment FROM public.indexing_indexdeployment WHERE id=1 FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO root FROM public.roots_root WHERE id=discovered_root FOR UPDATE SKIP LOCKED;
    IF NOT FOUND OR NOT root.active OR NOT deployment.slot_ids ? root.slot_id THEN RETURN NULL; END IF;
    SELECT * INTO root_state FROM public.indexing_rootindexstate WHERE root_id=root.id FOR UPDATE;
    IF NOT FOUND OR root_state.active_run_id IS DISTINCT FROM p_run_id
       OR root_state.binding_epoch <> deployment.epoch OR root_state.policy_epoch <> deployment.epoch
       THEN RETURN NULL; END IF;
    SELECT * INTO run FROM public.indexing_scanrun WHERE id=p_run_id FOR UPDATE;
    IF NOT FOUND OR run.root_id <> root.id OR run.state NOT IN ('queued','running')
       OR run.binding_epoch <> deployment.epoch OR run.policy_epoch <> deployment.epoch
       OR run.root_epoch <> root.authorization_epoch
       OR run.start_epoch <> root_state.reconciliation_epoch
       OR run.manifest_identity <> deployment.manifest_identity THEN RETURN NULL; END IF;
    database_now := pg_catalog.clock_timestamp();
    IF NOT EXISTS (
        SELECT 1 FROM public.operations_workerheartbeat AS h WHERE h.role='indexer'
        AND h.worker_id=p_worker_id AND h.manifest_identity=deployment.manifest_identity
        AND h.status IN ('idle','running') AND h.last_seen_at <= database_now
        AND h.last_seen_at > database_now - interval '120 seconds'
    ) THEN RETURN NULL; END IF;
    IF EXISTS (SELECT 1 FROM public.indexing_directorywork WHERE run_id=p_run_id
        AND state IN ('reading','finalizing') AND lease_expires_at > database_now) THEN
        RETURN NULL;
    END IF;
    UPDATE public.indexing_directorywork SET state='degraded', error_code='scan_attempts_exhausted',
        lease_owner=NULL, lease_expires_at=NULL, updated_at=database_now
        WHERE run_id=p_run_id AND state IN ('reading','finalizing')
          AND lease_expires_at <= database_now AND attempt >= max_directory_attempts;
    SELECT * INTO work FROM public.indexing_directorywork WHERE run_id=p_run_id
        AND attempt < max_directory_attempts AND available_at <= database_now
        AND (state='pending' OR (state IN ('reading','finalizing')
            AND lease_expires_at + pg_catalog.make_interval(secs=>CASE WHEN attempt=1
                THEN first_retry_seconds ELSE second_retry_seconds END) <= database_now))
        ORDER BY available_at, id LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO directory FROM public.catalog_catalogentry WHERE id=work.directory_id FOR UPDATE;
    IF NOT FOUND OR directory.root_id <> root.id OR directory.kind <> 'directory' THEN RETURN NULL; END IF;
    database_now := pg_catalog.clock_timestamp();
    IF NOT EXISTS (
        SELECT 1 FROM public.operations_workerheartbeat AS h WHERE h.role='indexer'
        AND h.worker_id=p_worker_id AND h.manifest_identity=deployment.manifest_identity
        AND h.status IN ('idle','running') AND h.last_seen_at <= database_now
        AND h.last_seen_at > database_now - interval '120 seconds'
    ) THEN RETURN NULL; END IF;
    UPDATE public.indexing_directorywork SET state='reading', attempt=attempt+1,
        lease_owner=p_worker_id::uuid,
        lease_expires_at=database_now+pg_catalog.make_interval(secs=>lease_seconds),
        parent_revision=directory.source_revision, last_batch_sequence=0, last_batch_hash=NULL, observed_count=0,
        eof_identity=NULL, error_code=NULL, updated_at=database_now
        WHERE id=work.id RETURNING * INTO work;
    UPDATE public.indexing_scanrun SET state='running' WHERE id=run.id;
    UPDATE public.indexing_rootindexstate SET status='scanning', updated_at=database_now
        WHERE root_id=root.id;
    RETURN pg_catalog.jsonb_build_object(
        'run_id', run.id, 'work_id', work.id, 'root_id', root.id, 'directory_id', directory.id,
        'worker_id', p_worker_id, 'attempt', work.attempt, 'generation', run.generation,
        'start_epoch', run.start_epoch, 'binding_epoch', run.binding_epoch,
        'policy_epoch', run.policy_epoch, 'root_epoch', run.root_epoch,
        'parent_revision', work.parent_revision, 'manifest_identity', run.manifest_identity);
END;
$claim$;

CREATE OR REPLACE FUNCTION public.aegis_renew_scan_directory(p_lease jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $renew$
DECLARE
    deployment public.indexing_indexdeployment%ROWTYPE;
    root public.roots_root%ROWTYPE;
    root_state public.indexing_rootindexstate%ROWTYPE;
    run public.indexing_scanrun%ROWTYPE;
    work public.indexing_directorywork%ROWTYPE;
    directory public.catalog_catalogentry%ROWTYPE;
    item record;
    database_now timestamptz;
    lease_seconds CONSTANT integer := 60;
BEGIN
    /* AEGIS_LEASE_FENCE_BEGIN */
    IF session_user <> 'aegis_indexer' THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    IF p_lease IS NULL OR pg_catalog.jsonb_typeof(p_lease) <> 'object'
       OR pg_catalog.octet_length(p_lease::text) > 8192 THEN
        RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_lease)) <> 13 THEN
        RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023';
    END IF;
    FOR item IN SELECT * FROM pg_catalog.jsonb_each(p_lease) LOOP
        IF item.key IN ('run_id','work_id','root_id','directory_id','worker_id') THEN
            IF pg_catalog.jsonb_typeof(item.value) <> 'string'
               OR p_lease->>item.key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               THEN RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023'; END IF;
        ELSIF item.key IN ('attempt','generation','start_epoch','binding_epoch','policy_epoch',
                           'root_epoch','parent_revision') THEN
            IF pg_catalog.jsonb_typeof(item.value) <> 'number'
               OR p_lease->>item.key !~ '^[0-9]+$' THEN
                RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023';
            END IF;
            IF (p_lease->>item.key)::numeric > 9223372036854775807 THEN
                RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023';
            END IF;
        ELSIF item.key='manifest_identity' THEN
            IF pg_catalog.jsonb_typeof(item.value) <> 'string'
               OR p_lease->>item.key !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023';
            END IF;
        ELSE RAISE EXCEPTION 'invalid scan lease' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    SELECT * INTO deployment FROM public.indexing_indexdeployment WHERE id=1 FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO root FROM public.roots_root WHERE id=(p_lease->>'root_id')::uuid FOR UPDATE;
    IF NOT FOUND OR NOT root.active OR NOT deployment.slot_ids ? root.slot_id
        OR root.authorization_epoch <> (p_lease->>'root_epoch')::bigint THEN RETURN false; END IF;
    SELECT * INTO root_state FROM public.indexing_rootindexstate WHERE root_id=root.id FOR UPDATE;
    IF NOT FOUND OR root_state.active_run_id IS DISTINCT FROM (p_lease->>'run_id')::uuid
       OR root_state.binding_epoch <> deployment.epoch OR root_state.policy_epoch <> deployment.epoch
       THEN RETURN false; END IF;
    SELECT * INTO run FROM public.indexing_scanrun WHERE id=(p_lease->>'run_id')::uuid FOR UPDATE;
    IF NOT FOUND OR run.root_id <> root.id OR run.state <> 'running'
       OR run.binding_epoch <> deployment.epoch OR run.policy_epoch <> deployment.epoch
       OR run.binding_epoch <> (p_lease->>'binding_epoch')::bigint
       OR run.policy_epoch <> (p_lease->>'policy_epoch')::bigint
       OR run.root_epoch <> root.authorization_epoch
       OR run.start_epoch <> root_state.reconciliation_epoch
       OR run.start_epoch <> (p_lease->>'start_epoch')::bigint
       OR run.generation <> (p_lease->>'generation')::bigint
       OR run.manifest_identity <> deployment.manifest_identity
       OR run.manifest_identity <> p_lease->>'manifest_identity' THEN RETURN false; END IF;
    SELECT * INTO work FROM public.indexing_directorywork WHERE id=(p_lease->>'work_id')::uuid FOR UPDATE;
    IF NOT FOUND OR work.run_id <> run.id OR work.directory_id <> (p_lease->>'directory_id')::uuid
       OR work.state NOT IN ('reading','finalizing')
       OR work.attempt <> (p_lease->>'attempt')::bigint
       OR work.lease_owner IS DISTINCT FROM (p_lease->>'worker_id')::uuid
       OR work.parent_revision <> (p_lease->>'parent_revision')::bigint THEN RETURN false; END IF;
    SELECT * INTO directory FROM public.catalog_catalogentry WHERE id=work.directory_id FOR UPDATE;
    IF NOT FOUND OR directory.root_id <> root.id OR directory.kind <> 'directory'
       OR directory.source_revision <> work.parent_revision THEN RETURN false; END IF;
    -- Bounded source ancestry, independent of logical organization. A missing
    -- parent, unknown capture, replacement or cycle cannot reach the root anchor.
    IF NOT EXISTS (
        WITH RECURSIVE ancestry AS (
            SELECT entry.id, entry.source_parent_id, entry.source_parent_revision,
                   ARRAY[entry.id] AS visited
              FROM public.catalog_catalogentry AS entry
             WHERE entry.id=directory.id AND entry.root_id=root.id
               AND entry.kind='directory' AND entry.source_state='present'
            UNION ALL
            SELECT parent.id, parent.source_parent_id, parent.source_parent_revision,
                   ancestry.visited || parent.id
              FROM ancestry JOIN public.catalog_catalogentry AS parent
                ON parent.id=ancestry.source_parent_id AND parent.root_id=root.id
               AND parent.source_revision=ancestry.source_parent_revision
             WHERE parent.kind='directory' AND parent.source_state='present'
               AND NOT parent.id=ANY(ancestry.visited)
               AND pg_catalog.cardinality(ancestry.visited)<257
        ) SELECT 1 FROM ancestry WHERE source_parent_id IS NULL
    ) THEN RETURN false; END IF;
    -- Read time after all potentially waiting locks: an expired lease never revives.
    database_now := pg_catalog.clock_timestamp();
    IF work.lease_expires_at IS NULL OR work.lease_expires_at <= database_now
       OR work.lease_expires_at > database_now+pg_catalog.make_interval(secs=>lease_seconds)
       OR NOT EXISTS (SELECT 1 FROM public.operations_workerheartbeat AS h
           WHERE h.role='indexer' AND h.worker_id=p_lease->>'worker_id'
           AND h.manifest_identity=deployment.manifest_identity AND h.status IN ('idle','running')
           AND h.last_seen_at <= database_now
           AND h.last_seen_at > database_now - interval '120 seconds') THEN RETURN false; END IF;
    /* AEGIS_LEASE_FENCE_END */
    UPDATE public.indexing_directorywork SET
        lease_expires_at=database_now+pg_catalog.make_interval(secs=>lease_seconds), updated_at=database_now
        WHERE id=work.id;
    RETURN true;
END;
$renew$;
