CREATE OR REPLACE FUNCTION public.aegis_record_scan_batch(p_lease jsonb, p_batch jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $record$
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
    observation jsonb;
    field text;
    raw_bytes bytea;
    name_bytes bytea;
    sequence_number bigint;
    batch_hash text;
    observed integer;
    inserted integer;
    changed integer;
    has_errors boolean := false;
BEGIN
    /* AEGIS_CHECKPOINT_FENCE_NULL */
    IF work.state <> 'reading' THEN RETURN NULL; END IF;
    IF p_batch IS NULL OR pg_catalog.jsonb_typeof(p_batch) <> 'object'
       OR pg_catalog.octet_length(p_batch::text) > 1048576
       OR NOT p_batch ?& ARRAY['sequence','observations']
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_batch)) <> 2
       OR pg_catalog.jsonb_typeof(p_batch->'sequence') <> 'number'
       OR p_batch->>'sequence' !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_batch->'observations') <> 'array' THEN
        RAISE EXCEPTION 'invalid scan batch' USING ERRCODE='22023';
    END IF;
    IF (p_batch->>'sequence')::numeric > 2147483647 THEN
        RAISE EXCEPTION 'invalid scan batch' USING ERRCODE='22023';
    END IF;
    sequence_number := (p_batch->>'sequence')::bigint;
    observed := pg_catalog.jsonb_array_length(p_batch->'observations');
    IF observed NOT BETWEEN 1 AND deployment.batch_records THEN
        RAISE EXCEPTION 'invalid scan batch' USING ERRCODE='22023';
    END IF;
    FOR observation IN SELECT value FROM pg_catalog.jsonb_array_elements(p_batch->'observations') LOOP
        IF pg_catalog.jsonb_typeof(observation) <> 'object' THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END IF;
        IF NOT observation ?& ARRAY['raw','display','name_key','type_hint','kind','state',
                                    'size','mtime_ns','ctime_ns','device','inode']
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(observation)) <> 11 THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END IF;
        FOREACH field IN ARRAY ARRAY['raw','display','name_key','kind','state'] LOOP
            IF pg_catalog.jsonb_typeof(observation->field) <> 'string' THEN
                RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
            END IF;
        END LOOP;
        IF pg_catalog.length(observation->>'raw') NOT BETWEEN 4 AND 340
           OR observation->>'raw' !~ '^[A-Za-z0-9+/]*={0,2}$'
           OR pg_catalog.length(observation->>'name_key') NOT BETWEEN 4 AND 2732
           OR observation->>'name_key' !~ '^[A-Za-z0-9+/]*={0,2}$' THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END IF;
        BEGIN
            raw_bytes := pg_catalog.decode(observation->>'raw','base64');
            name_bytes := pg_catalog.decode(observation->>'name_key','base64');
        EXCEPTION WHEN invalid_parameter_value OR invalid_text_representation THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END;
        IF pg_catalog.replace(pg_catalog.encode(raw_bytes,'base64'),E'\n','') <> observation->>'raw'
           OR pg_catalog.replace(pg_catalog.encode(name_bytes,'base64'),E'\n','') <> observation->>'name_key'
           OR pg_catalog.octet_length(raw_bytes) NOT BETWEEN 1 AND 255
           OR raw_bytes IN ('.'::bytea,'..'::bytea)
           OR position(pg_catalog.decode('00','hex') IN raw_bytes) > 0
           OR position('/'::bytea IN raw_bytes) > 0
           OR pg_catalog.octet_length(name_bytes) NOT BETWEEN 1 AND 2048
           OR pg_catalog.octet_length(observation->>'display') NOT BETWEEN 1 AND 1530
           OR observation->>'kind' NOT IN ('directory','file','symlink','special')
           OR observation->>'state' NOT IN ('present','inaccessible','unsupported') THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END IF;
        IF observation->'type_hint' <> 'null'::jsonb AND (
            pg_catalog.jsonb_typeof(observation->'type_hint') <> 'string'
            OR observation->>'type_hint' !~ '^[a-z0-9]{1,16}$') THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END IF;
        FOREACH field IN ARRAY ARRAY['size','mtime_ns','ctime_ns','device','inode'] LOOP
            IF observation->>'state' = 'inaccessible' THEN
                IF observation->field <> 'null'::jsonb THEN
                    RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
                END IF;
            ELSE
                IF pg_catalog.jsonb_typeof(observation->field) <> 'number'
                   OR observation->>field !~ '^-?[0-9]+$' THEN
                    RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
                END IF;
                IF (observation->>field)::numeric < (CASE WHEN field='inode' THEN 1
                       WHEN field IN ('size','device') THEN 0 ELSE -9223372036854775808 END)
                   OR (observation->>field)::numeric > (CASE WHEN field IN ('device','inode')
                       THEN 18446744073709551615 ELSE 9223372036854775807 END) THEN
                    RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
                END IF;
            END IF;
        END LOOP;
        IF (observation->>'state'='unsupported' AND observation->>'kind'<>'special')
           OR (observation->>'kind'='special' AND observation->>'state'='present') THEN
            RAISE EXCEPTION 'invalid scan observation' USING ERRCODE='22023';
        END IF;
        has_errors := has_errors OR observation->>'state'='inaccessible';
    END LOOP;
    IF (SELECT pg_catalog.count(DISTINCT value->>'raw')
        FROM pg_catalog.jsonb_array_elements(p_batch->'observations')) <> observed THEN
        RAISE EXCEPTION 'invalid scan batch' USING ERRCODE='22023';
    END IF;
    batch_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_batch::text,'UTF8')),'hex');
    IF sequence_number = work.last_batch_sequence THEN
        IF batch_hash IS DISTINCT FROM work.last_batch_hash THEN
            RAISE EXCEPTION 'invalid scan replay' USING ERRCODE='22023';
        END IF;
        -- Even acknowledgements must commit against a current work fence.
        UPDATE public.indexing_directorywork SET updated_at=database_now WHERE id=work.id;
        RETURN pg_catalog.jsonb_build_object('observed',0,'inserted',0,'changed',0);
    END IF;
    IF sequence_number <> work.last_batch_sequence+1 THEN
        RAISE EXCEPTION 'invalid scan sequence' USING ERRCODE='22023';
    END IF;
    WITH upserted AS (
        INSERT INTO public.catalog_catalogentry AS target (
            id, root_id, source_parent_id, source_parent_revision, logical_parent_id,
            raw_name, display_name, name_key, logical_name, kind, type_hint, source_state,
            size, mtime_ns, ctime_ns, device, inode, source_revision, catalog_version,
            children_version, observation_epoch, seen_generation, seen_attempt, observed_at
        ) SELECT pg_catalog.gen_random_uuid(), root.id, directory.id, directory.source_revision,
            directory.id, pg_catalog.decode(v->>'raw','base64'), v->>'display',
            pg_catalog.decode(v->>'name_key','base64'), NULL, v->>'kind', v->>'type_hint',
            v->>'state', (v->>'size')::numeric, (v->>'mtime_ns')::numeric,
            (v->>'ctime_ns')::numeric, (v->>'device')::numeric, (v->>'inode')::numeric,
            0, 0, 0, run.start_epoch, run.generation, work.attempt, database_now
          FROM pg_catalog.jsonb_array_elements(p_batch->'observations') AS batch(v)
        ON CONFLICT (root_id, source_parent_id, raw_name) WHERE source_parent_id IS NOT NULL
        DO UPDATE SET
            display_name=excluded.display_name, name_key=excluded.name_key, type_hint=excluded.type_hint,
            source_parent_revision=excluded.source_parent_revision,
            kind=CASE WHEN excluded.source_state='inaccessible' THEN target.kind ELSE excluded.kind END,
            source_state=excluded.source_state,
            size=CASE WHEN excluded.source_state='inaccessible' THEN target.size ELSE excluded.size END,
            mtime_ns=CASE WHEN excluded.source_state='inaccessible' THEN target.mtime_ns ELSE excluded.mtime_ns END,
            ctime_ns=CASE WHEN excluded.source_state='inaccessible' THEN target.ctime_ns ELSE excluded.ctime_ns END,
            device=CASE WHEN excluded.source_state='inaccessible' THEN target.device ELSE excluded.device END,
            inode=CASE WHEN excluded.source_state='inaccessible' THEN target.inode ELSE excluded.inode END,
            source_revision=target.source_revision + CASE WHEN
                target.source_state IS DISTINCT FROM excluded.source_state OR
                target.source_parent_revision IS DISTINCT FROM excluded.source_parent_revision OR
                (excluded.source_state<>'inaccessible' AND
                 (target.kind,target.size,target.mtime_ns,target.ctime_ns,target.device,target.inode)
                 IS DISTINCT FROM
                 (excluded.kind,excluded.size,excluded.mtime_ns,excluded.ctime_ns,excluded.device,excluded.inode))
                THEN 1 ELSE 0 END,
            catalog_version=target.catalog_version + CASE WHEN
                target.source_state IS DISTINCT FROM excluded.source_state OR
                target.source_parent_revision IS DISTINCT FROM excluded.source_parent_revision OR
                (excluded.source_state<>'inaccessible' AND
                 (target.kind,target.size,target.mtime_ns,target.ctime_ns,target.device,target.inode)
                 IS DISTINCT FROM
                 (excluded.kind,excluded.size,excluded.mtime_ns,excluded.ctime_ns,excluded.device,excluded.inode))
                THEN 1 ELSE 0 END,
            observation_epoch=excluded.observation_epoch, seen_generation=excluded.seen_generation,
            seen_attempt=excluded.seen_attempt, observed_at=excluded.observed_at
        WHERE target.observation_epoch <= run.start_epoch
        RETURNING WITH (OLD AS previous, NEW AS current)
            current.id, current.kind, current.source_state, current.source_revision,
            previous.id IS NULL AS was_inserted,
            previous.id IS NOT NULL AND previous.source_revision <> current.source_revision AS was_changed
    ), children AS (
        INSERT INTO public.indexing_directorywork (
            id, run_id, directory_id, parent_revision, state, attempt, lease_owner,
            lease_expires_at, available_at, last_batch_sequence, last_batch_hash, observed_count,
            eof_identity, error_code, updated_at
        ) SELECT pg_catalog.gen_random_uuid(), run.id, id, source_revision, 'pending', 0,
            NULL, NULL, database_now, 0, NULL, 0, NULL, NULL, database_now
            FROM upserted WHERE kind='directory' AND source_state='present'
        ON CONFLICT (run_id,directory_id) DO UPDATE SET state='pending',
            parent_revision=excluded.parent_revision, lease_owner=NULL, lease_expires_at=NULL,
            eof_identity=NULL, error_code=NULL, updated_at=database_now
            WHERE public.indexing_directorywork.parent_revision<>excluded.parent_revision
        RETURNING id
    ) SELECT pg_catalog.count(*) FILTER (WHERE was_inserted),
             pg_catalog.count(*) FILTER (WHERE was_changed) INTO inserted, changed FROM upserted;
    UPDATE public.indexing_directorywork SET last_batch_sequence=sequence_number,
        last_batch_hash=batch_hash, observed_count=observed_count+observed,
        error_code=CASE WHEN has_errors THEN 'permission_denied' ELSE error_code END,
        updated_at=database_now WHERE id=work.id;
    UPDATE public.indexing_rootindexstate SET observed_entries=observed_entries+observed,
        updated_at=database_now WHERE root_id=root.id;
    RETURN pg_catalog.jsonb_build_object('observed',observed,'inserted',inserted,'changed',changed);
END;
$record$;
