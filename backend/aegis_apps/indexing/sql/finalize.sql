CREATE OR REPLACE FUNCTION public.aegis_seal_scan_directory(p_lease jsonb, p_identity jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $seal$
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
    position integer;
BEGIN
    /* AEGIS_CHECKPOINT_FENCE_FALSE */
    IF p_identity IS NULL OR pg_catalog.jsonb_typeof(p_identity)<>'array'
       OR pg_catalog.octet_length(p_identity::text)>256 THEN
        RAISE EXCEPTION 'invalid scan identity' USING ERRCODE='22023';
    END IF;
    IF pg_catalog.jsonb_array_length(p_identity)<>4 THEN
        RAISE EXCEPTION 'invalid scan identity' USING ERRCODE='22023';
    END IF;
    FOR position IN 0..3 LOOP
        IF pg_catalog.jsonb_typeof(p_identity->position)<>'number'
           OR p_identity->>position !~ '^-?[0-9]+$' THEN
            RAISE EXCEPTION 'invalid scan identity' USING ERRCODE='22023';
        END IF;
        IF (p_identity->>position)::numeric < (CASE WHEN position=0 THEN 0
               WHEN position=1 THEN 1 ELSE -9223372036854775808 END)
           OR (p_identity->>position)::numeric > (CASE WHEN position<2
               THEN 18446744073709551615 ELSE 9223372036854775807 END) THEN
            RAISE EXCEPTION 'invalid scan identity' USING ERRCODE='22023';
        END IF;
    END LOOP;
    IF work.state='finalizing' THEN
        IF work.eof_identity IS DISTINCT FROM p_identity THEN RETURN false; END IF;
        -- Replayed success must also reach the deferred commit fence.
        UPDATE public.indexing_directorywork SET updated_at=database_now WHERE id=work.id;
        RETURN true;
    END IF;
    IF work.error_code IS NOT NULL OR (directory.source_parent_id IS NOT NULL AND
        (directory.device,directory.inode,directory.mtime_ns,directory.ctime_ns) IS DISTINCT FROM
        ((p_identity->>0)::numeric,(p_identity->>1)::numeric,
         (p_identity->>2)::numeric,(p_identity->>3)::numeric)) THEN
        UPDATE public.indexing_directorywork SET state='degraded',
            error_code=COALESCE(error_code,'identity_changed'), updated_at=database_now WHERE id=work.id;
        UPDATE public.indexing_rootindexstate SET status='degraded',
            degraded_directories=degraded_directories+1,
            updated_at=database_now WHERE root_id=root.id;
        RETURN false;
    END IF;
    -- The synthetic anchor is not a physical observation. Do not bump its revision
    -- at EOF and thereby invalidate the children just stamped with that revision.
    UPDATE public.indexing_directorywork SET state='finalizing', eof_identity=p_identity,
        updated_at=database_now WHERE id=work.id;
    RETURN true;
END;
$seal$;

CREATE OR REPLACE FUNCTION public.aegis_finalize_scan_directory(p_lease jsonb, batch_limit integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $finalize$
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
    root_uuid uuid;
    directory_uuid uuid;
    run_generation bigint;
    work_attempt bigint;
    captured_epoch bigint;
    affected integer;
    finished boolean;
BEGIN
    /* AEGIS_CHECKPOINT_FENCE_NULL */
    IF batch_limit IS NULL OR batch_limit NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'invalid finalization limit' USING ERRCODE='22023';
    END IF;
    IF work.state<>'finalizing' OR work.eof_identity IS NULL OR work.error_code IS NOT NULL THEN
        RETURN NULL;
    END IF;
    root_uuid := root.id;
    directory_uuid := directory.id;
    run_generation := run.generation;
    work_attempt := work.attempt;
    captured_epoch := run.start_epoch;
    WITH candidates AS (
        SELECT entry.id FROM public.catalog_catalogentry AS entry
         WHERE entry.root_id=root_uuid AND entry.source_parent_id=directory_uuid
           AND (entry.seen_generation,entry.seen_attempt)<>(run_generation,work_attempt)
           AND entry.observation_epoch<=captured_epoch AND entry.source_state<>'missing'
         ORDER BY entry.id LIMIT batch_limit FOR UPDATE
    ) UPDATE public.catalog_catalogentry AS entry
         SET source_state='missing', catalog_version=entry.catalog_version+1
        FROM candidates WHERE entry.id=candidates.id AND entry.observation_epoch<=captured_epoch;
    GET DIAGNOSTICS affected = ROW_COUNT;
    SELECT NOT EXISTS (
        SELECT 1 FROM public.catalog_catalogentry AS entry
         WHERE entry.root_id=root_uuid AND entry.source_parent_id=directory_uuid
           AND (entry.seen_generation,entry.seen_attempt)<>(run_generation,work_attempt)
           AND entry.observation_epoch<=captured_epoch AND entry.source_state<>'missing'
    ) INTO finished;
    UPDATE public.indexing_directorywork SET state=CASE WHEN finished THEN 'complete' ELSE state END,
        updated_at=database_now WHERE id=work.id;
    IF finished THEN
        UPDATE public.indexing_rootindexstate SET completed_directories=completed_directories+1,
            updated_at=database_now WHERE root_id=root_uuid;
    END IF;
    RETURN pg_catalog.jsonb_build_object('affected',affected,'complete',finished);
END;
$finalize$;

CREATE OR REPLACE FUNCTION public.aegis_fail_scan_directory(p_lease jsonb, p_code text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $fail$
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
    /* AEGIS_CHECKPOINT_FENCE_FALSE */
    IF p_code IS NULL OR p_code NOT IN ('source_unavailable','identity_changed','permission_denied',
        'unsupported_entry','reader_timeout','reader_protocol_error') THEN
        RAISE EXCEPTION 'invalid scan failure' USING ERRCODE='22023';
    END IF;
    UPDATE public.indexing_directorywork SET state='degraded', error_code=p_code,
        eof_identity=NULL, updated_at=database_now WHERE id=work.id;
    UPDATE public.indexing_rootindexstate SET status='degraded',
        degraded_directories=degraded_directories+1,
        updated_at=database_now WHERE root_id=root.id;
    RETURN true;
END;
$fail$;
