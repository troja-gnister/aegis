CREATE OR REPLACE FUNCTION public.aegis_schedule_root_scan(
    p_root_id uuid, p_worker_id text, p_manifest_identity text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $scan$
DECLARE
    deployment public.indexing_indexdeployment%ROWTYPE;
    root public.roots_root%ROWTYPE;
    root_state public.indexing_rootindexstate%ROWTYPE;
    run public.indexing_scanrun%ROWTYPE;
    anchor_id uuid;
    anchor_revision bigint;
    database_now timestamptz;
    max_directory_attempts CONSTANT integer := 3;
BEGIN
    IF session_user <> 'aegis_indexer' THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    IF p_root_id IS NULL OR p_worker_id IS NULL OR p_manifest_identity IS NULL
       OR p_worker_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR p_manifest_identity !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid scan request' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO deployment FROM public.indexing_indexdeployment WHERE id=1 FOR SHARE;
    IF NOT FOUND OR deployment.manifest_identity <> p_manifest_identity THEN RETURN NULL; END IF;
    SELECT * INTO root FROM public.roots_root WHERE id=p_root_id FOR UPDATE SKIP LOCKED;
    IF NOT FOUND OR NOT root.active OR NOT deployment.slot_ids ? root.slot_id THEN
        RETURN NULL;
    END IF;
    database_now := pg_catalog.clock_timestamp();
    IF NOT EXISTS (
        SELECT 1 FROM public.operations_workerheartbeat AS h
        WHERE h.role='indexer' AND h.worker_id=p_worker_id
          AND h.manifest_identity=deployment.manifest_identity AND h.status IN ('idle','running')
          AND h.last_seen_at <= database_now
          AND h.last_seen_at > database_now - interval '120 seconds'
    ) THEN RETURN NULL; END IF;

    INSERT INTO public.indexing_rootindexstate (
        root_id, binding_epoch, policy_epoch, reconciliation_epoch, next_generation,
        due_at, active_run_id, rescan_requested, status, observed_entries,
        completed_directories, degraded_directories, updated_at, last_completed_at
    ) VALUES (p_root_id, deployment.epoch, deployment.epoch, 0, 1,
        database_now, NULL, false, 'not_indexed', 0, 0, 0, database_now, NULL)
    ON CONFLICT (root_id) DO NOTHING;
    SELECT * INTO root_state FROM public.indexing_rootindexstate
        WHERE root_id=p_root_id FOR UPDATE;
    IF root_state.active_run_id IS NOT NULL THEN
        SELECT * INTO run FROM public.indexing_scanrun
            WHERE id=root_state.active_run_id FOR UPDATE;
        IF run.state IN ('queued','running') THEN
            IF run.binding_epoch <> deployment.epoch OR run.policy_epoch <> deployment.epoch
               OR run.root_epoch <> root.authorization_epoch
               OR run.manifest_identity <> deployment.manifest_identity
               OR run.start_epoch <> root_state.reconciliation_epoch THEN
                UPDATE public.indexing_scanrun SET state='fenced', settled_at=database_now
                    WHERE id=run.id;
                root_state.rescan_requested := true;
            ELSE
                UPDATE public.indexing_directorywork SET state='degraded',
                    error_code='scan_attempts_exhausted', lease_owner=NULL,
                    lease_expires_at=NULL, updated_at=database_now
                    WHERE run_id=run.id AND state IN ('reading','finalizing')
                      AND attempt >= max_directory_attempts AND lease_expires_at <= database_now;
                IF EXISTS (SELECT 1 FROM public.indexing_directorywork
                    WHERE run_id=run.id AND state IN ('pending','reading','finalizing')) THEN
                    RETURN run.id;
                END IF;
                UPDATE public.indexing_scanrun SET
                    state=CASE WHEN EXISTS (SELECT 1 FROM public.indexing_directorywork
                        WHERE run_id=run.id AND state='degraded') THEN 'degraded' ELSE 'complete' END,
                    settled_at=database_now WHERE id=run.id RETURNING * INTO run;
            END IF;
        END IF;
        root_state.active_run_id := NULL;
        root_state.due_at := CASE WHEN root_state.rescan_requested THEN database_now
            ELSE COALESCE(run.settled_at, database_now)
                 + pg_catalog.make_interval(secs=>deployment.interval_seconds) END;
        UPDATE public.indexing_rootindexstate SET active_run_id=NULL,
            due_at=root_state.due_at, rescan_requested=root_state.rescan_requested,
            status=CASE WHEN run.state='complete' THEN 'ready' ELSE 'degraded' END,
            last_completed_at=CASE WHEN run.state='complete' THEN run.settled_at
                ELSE last_completed_at END, updated_at=database_now WHERE root_id=p_root_id;
    END IF;
    IF root_state.due_at > database_now AND NOT root_state.rescan_requested THEN RETURN NULL; END IF;

    /* AEGIS_START_PERIODIC_RUN */
    UPDATE public.indexing_rootindexstate SET binding_epoch=deployment.epoch,
        policy_epoch=deployment.epoch, active_run_id=run.id,
        next_generation=root_state.next_generation+1, rescan_requested=false,
        status='queued', updated_at=database_now WHERE root_id=p_root_id;
    RETURN run.id;
END;
$scan$;

CREATE OR REPLACE FUNCTION public.aegis_request_root_scan(
    p_root_id uuid, p_actor_id uuid, p_actor_epoch bigint, p_request_id text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $request$
DECLARE
    deployment public.indexing_indexdeployment%ROWTYPE;
    root public.roots_root%ROWTYPE;
    actor public.identity_user%ROWTYPE;
    root_state public.indexing_rootindexstate%ROWTYPE;
    run public.indexing_scanrun%ROWTYPE;
    prior public.indexing_scanrequest%ROWTYPE;
    permission_mask integer;
    database_now timestamptz;
    anchor_id uuid;
    anchor_revision bigint;
BEGIN
    IF session_user <> 'aegis_web' THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    IF p_root_id IS NULL OR p_actor_id IS NULL OR p_actor_epoch IS NULL OR p_actor_epoch < 0
       OR p_request_id IS NULL OR p_request_id !~ '^[A-Za-z0-9_-]{8,64}$' THEN
        RAISE EXCEPTION 'invalid scan request' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO deployment FROM public.indexing_indexdeployment WHERE id=1 FOR SHARE;
    IF NOT FOUND OR deployment.manifest_identity !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO root FROM public.roots_root WHERE id=p_root_id FOR UPDATE;
    IF NOT FOUND OR NOT root.active OR NOT deployment.slot_ids ? root.slot_id THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO actor FROM public.identity_user WHERE id=p_actor_id FOR UPDATE;
    IF NOT FOUND OR NOT actor.is_active OR actor.authorization_epoch <> p_actor_epoch THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    SELECT COALESCE(pg_catalog.bit_or(g.permissions), 0) INTO permission_mask
        FROM public.roots_rootgrant AS g WHERE g.root_id=p_root_id AND
        (g.user_id=p_actor_id OR g.group_id IN
            (SELECT m.group_id FROM public.identity_user_groups AS m WHERE m.user_id=p_actor_id));
    IF permission_mask & 128 <> 128 THEN
        RAISE EXCEPTION 'scan authority denied' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO prior FROM public.indexing_scanrequest
        WHERE actor_id=p_actor_id AND client_request_id=p_request_id;
    IF FOUND THEN
        IF prior.root_id <> p_root_id THEN
            RAISE EXCEPTION 'invalid scan request' USING ERRCODE = '22023';
        END IF;
        RETURN prior.run_id;
    END IF;
    database_now := pg_catalog.clock_timestamp();
    IF (SELECT pg_catalog.count(*) FROM (SELECT id FROM public.indexing_scanrequest
        WHERE actor_id=p_actor_id AND root_id=p_root_id
          AND created_at > database_now - interval '60 seconds' LIMIT 20) AS recent) >= 20 THEN
        RAISE EXCEPTION 'scan request rate limit exceeded' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO public.indexing_rootindexstate (
        root_id, binding_epoch, policy_epoch, reconciliation_epoch, next_generation,
        due_at, active_run_id, rescan_requested, status, observed_entries,
        completed_directories, degraded_directories, updated_at, last_completed_at
    ) VALUES (p_root_id, deployment.epoch, deployment.epoch, 0, 1,
        database_now, NULL, false, 'not_indexed', 0, 0, 0, database_now, NULL)
    ON CONFLICT (root_id) DO NOTHING;
    SELECT * INTO root_state FROM public.indexing_rootindexstate WHERE root_id=p_root_id FOR UPDATE;
    IF root_state.active_run_id IS NOT NULL THEN
        SELECT * INTO run FROM public.indexing_scanrun WHERE id=root_state.active_run_id FOR UPDATE;
        IF run.state IN ('queued','running') AND run.binding_epoch=deployment.epoch
           AND run.policy_epoch=deployment.epoch AND run.root_epoch=root.authorization_epoch
           AND run.start_epoch=root_state.reconciliation_epoch
           AND run.manifest_identity=deployment.manifest_identity THEN
            UPDATE public.indexing_rootindexstate SET rescan_requested=true, updated_at=database_now
                WHERE root_id=p_root_id;
        ELSE
            UPDATE public.indexing_scanrun SET state='fenced', settled_at=database_now
                WHERE id=run.id AND state IN ('queued','running');
            run.id := NULL;
        END IF;
    END IF;
    IF run.id IS NULL THEN
        /* AEGIS_START_MANUAL_RUN */
        UPDATE public.indexing_rootindexstate SET binding_epoch=deployment.epoch,
            policy_epoch=deployment.epoch, active_run_id=run.id, due_at=database_now,
            next_generation=root_state.next_generation+1, rescan_requested=false,
            status='queued', updated_at=database_now WHERE root_id=p_root_id;
    END IF;
    INSERT INTO public.indexing_scanrequest (id, root_id, actor_id, client_request_id, run_id, created_at)
        VALUES (pg_catalog.gen_random_uuid(), p_root_id, p_actor_id, p_request_id, run.id, database_now);
    INSERT INTO public.audit_auditevent (
        id, occurred_at, event_type, outcome, actor_id, request_id, root_id, object_id, metadata
    ) VALUES (pg_catalog.gen_random_uuid(), database_now, 'index.scan.requested', 'success',
        p_actor_id, p_request_id, p_root_id, run.id, '{}'::jsonb);
    RETURN run.id;
END;
$request$;
