-- ulduar-kind: additive
-- ulduar-min-library: 0.1.0
-- Step 2, archive: what Hodir records about the objects it stores (registrations, retention rules, deletions,
-- verifications), and the history retention that learns about them.
-- Runs as the ledger role (the owner of schema ulduar), inside the ledger's transaction.

-- Archive registrations: an artifact whose object Hodir stored. One row per artifact; scope and event_at are the
-- artifact's own (a trigger checks it). No foreign key to the partitioned artifact table, so making a partition
-- never locks this one.
CREATE TABLE ulduar.archive_registration (
  project_id      uuid NOT NULL,
  scope           text NOT NULL,
  artifact_id     uuid NOT NULL,
  event_at        timestamptz NOT NULL,
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  retention_class text CHECK (retention_class ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),  -- NULL: kept for ever
  PRIMARY KEY (project_id, artifact_id),
  FOREIGN KEY (project_id, scope) REFERENCES ulduar.scope
);
CREATE INDEX ON ulduar.archive_registration (project_id, scope);

CREATE TABLE ulduar.archive_retention_rule (   -- appended by the ledger role; the latest per (project, class) wins
  project_id           uuid NOT NULL REFERENCES ulduar.project,
  rule_id              uuid NOT NULL CHECK (uuid_extract_version(rule_id) = 7),
  event_at             timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(rule_id)),
  recorded_at          timestamptz NOT NULL DEFAULT now(),
  retention_class      text NOT NULL CHECK (retention_class ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
  keep_days            integer CHECK (keep_days > 0),         -- NULL: keep for ever
  keep_newest          integer NOT NULL CHECK (keep_newest >= 0),
  shortening_confirmed boolean NOT NULL,
  PRIMARY KEY (project_id, rule_id)
);

CREATE TABLE ulduar.archive_deletion (         -- never ages out, never redacted
  project_id  uuid NOT NULL,
  deletion_id uuid NOT NULL CHECK (uuid_extract_version(deletion_id) = 7),
  event_at    timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(deletion_id)),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  scope       text NOT NULL,
  checksum    bytea NOT NULL CHECK (octet_length(checksum) = 32),
  size_bytes  bigint NOT NULL CHECK (size_bytes >= 0),
  stage       text NOT NULL CHECK (stage IN ('intent', 'confirmed', 'cancelled')),
  intent_id   uuid,                              -- NULL for an intent; the intent's id for its outcome
  cause       text NOT NULL CHECK (cause IN ('retention', 'incomplete_put', 'purge')),
  rule_id     uuid,                              -- retention: the archive_retention_rule applied (not a foreign key: a rebuild restores deletion records into an Archivum that may lack the rule)
  reason      text CHECK (reason IN ('personal_data', 'secret', 'recorded_by_mistake')),  -- purge only
  CHECK ((stage = 'intent') = (intent_id IS NULL)),
  CHECK ((cause = 'retention') = (rule_id IS NOT NULL)),
  CHECK ((cause = 'purge') = (reason IS NOT NULL)),
  PRIMARY KEY (project_id, deletion_id),
  UNIQUE (project_id, intent_id),                -- one outcome per intent
  FOREIGN KEY (project_id, scope) REFERENCES ulduar.scope,
  FOREIGN KEY (project_id, intent_id) REFERENCES ulduar.archive_deletion (project_id, deletion_id)
);
CREATE INDEX ON ulduar.archive_deletion (project_id, scope, checksum);

CREATE TABLE ulduar.archive_check (            -- one verification or reconciliation; monthly partitions
  project_id      uuid NOT NULL,
  check_id        uuid NOT NULL CHECK (uuid_extract_version(check_id) = 7),
  event_at        timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(check_id)),
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  scope           text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('verification', 'reconciliation')),
  mode            text NOT NULL CHECK (mode IN ('full', 'sample')),
  objects_checked bigint NOT NULL CHECK (objects_checked >= 0),
  findings        bigint NOT NULL CHECK (findings >= 0),   -- outcomes other than 'passed'
  PRIMARY KEY (project_id, check_id, event_at)
) PARTITION BY RANGE (event_at);

CREATE TABLE ulduar.archive_finding (          -- what a check found for one object; monthly partitions
  project_id  uuid NOT NULL,
  finding_id  uuid NOT NULL CHECK (uuid_extract_version(finding_id) = 7),
  event_at    timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(finding_id)),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  check_id    uuid NOT NULL,
  scope       text NOT NULL,
  checksum    bytea CHECK (octet_length(checksum) = 32),   -- NULL when the key names none (a stray temporary file)
  size_bytes  bigint CHECK (size_bytes >= 0),
  outcome     text NOT NULL CHECK (outcome IN ('passed', 'mismatch', 'missing', 'unreadable', 'not_immediate',
                                               'object_without_entry', 'entry_without_object', 'unfinished_upload')),
  PRIMARY KEY (project_id, finding_id, event_at)
) PARTITION BY RANGE (event_at);
CREATE INDEX ON ulduar.archive_finding (project_id, scope, checksum, event_at);

-- A registration names an artifact of its own project, scope and time. UL002: it does not (the code step 1's lineage
-- trigger uses for a node outside the project, which the library raises as UnknownRecordError).
CREATE FUNCTION ulduar.check_archive_registration() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, ulduar AS
$$
BEGIN
  IF NOT EXISTS (
    SELECT FROM ulduar.artifact
    WHERE project_id = NEW.project_id AND artifact_id = NEW.artifact_id AND scope = NEW.scope AND event_at = NEW.event_at
  ) THEN
    RAISE EXCEPTION 'ulduar: archive registration names artifact % that is not in its project, scope and time',
      NEW.artifact_id USING ERRCODE = 'UL002';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER check_registration BEFORE INSERT ON ulduar.archive_registration
  FOR EACH ROW EXECUTE FUNCTION ulduar.check_archive_registration();

-- True while the artifact's object is still in the archive as far as the Archivum knows: it has a registration
-- and no confirmed deletion of its content (project, scope, checksum) was recorded after that registration. Both
-- times are the database's clock (recorded_at, stamped by trigger), never a client's event_at. Retention of the
-- artifact history skips an artifact for which this is true.
CREATE FUNCTION ulduar.artifact_is_archived(p uuid, a uuid) RETURNS boolean LANGUAGE sql STABLE
  SET search_path = pg_catalog, ulduar AS
$$ SELECT EXISTS (
     SELECT FROM ulduar.archive_registration r
     JOIN ulduar.artifact x ON x.project_id = r.project_id AND x.artifact_id = r.artifact_id
                           AND x.event_at = r.event_at   -- the registration carries it: partition pruning
     WHERE r.project_id = p AND r.artifact_id = a
       AND NOT EXISTS (
         SELECT FROM ulduar.archive_deletion d
         WHERE d.project_id = r.project_id AND d.scope = r.scope AND d.checksum = x.checksum
           AND d.stage = 'confirmed' AND d.recorded_at > r.recorded_at)) $$;

CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.archive_registration FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.archive_retention_rule FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.archive_deletion FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.archive_check FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.archive_finding FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();

-- Append-only as in step 1: a registration leaves with its artifact's retention, the two check histories age
-- out by retention, a rule and a deletion record never change.
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.archive_registration FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change('DELETE:retention');
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.archive_check FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change('DELETE:retention');
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.archive_finding FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change('DELETE:retention');
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.archive_retention_rule FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.archive_deletion FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.archive_registration FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.archive_retention_rule FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.archive_deletion FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.archive_check FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.archive_finding FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();

-- The two check histories age out like the lineage edges: the rule's histories widen (additive).
ALTER TABLE ulduar.history_retention_rule DROP CONSTRAINT history_retention_rule_history_check,
  ADD CONSTRAINT history_retention_rule_history_check
  CHECK (history IN ('artifact', 'lineage_edge', 'archive_check', 'archive_finding'));

-- Step 1's partition functions, replaced to cover the two new partitioned tables (CREATE OR REPLACE keeps their
-- grants). Callers serialise on the same advisory lock as before. Creating a partition locks its parent, so the
-- lock order of every path is lineage_edge, artifact, then {dataset, scope, artifact_kind} (the foreign keys cloned
-- onto an artifact partition, locked while it is made), then archive_check, archive_finding. archive_registration
-- is in no such path: a registration insert reads artifact but the partition functions never lock the registration
-- table.
CREATE OR REPLACE FUNCTION ulduar.ensure_partitions(for_time timestamptz, months_ahead integer DEFAULT 3) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, ulduar SET TimeZone = 'UTC' SET DateStyle = 'ISO' AS
$$
DECLARE m timestamp; t text; part text; made integer := 0;
BEGIN
  IF months_ahead NOT BETWEEN 0 AND 24 THEN RAISE EXCEPTION 'ulduar: months_ahead out of range'; END IF;
  IF for_time IS NULL OR for_time NOT BETWEEN now() - interval '15 years' AND now() + interval '2 years' THEN
    RAISE EXCEPTION 'ulduar: partition time % is outside the window from 15 years ago to 2 years ahead', for_time;
  END IF;
  PERFORM pg_advisory_xact_lock(-6595867142526573570);
  FOREACH t IN ARRAY ARRAY['lineage_edge', 'artifact', 'archive_check', 'archive_finding'] LOOP
    FOR i IN 0..months_ahead LOOP
      m := date_trunc('month', for_time AT TIME ZONE 'UTC') + make_interval(months => i);
      part := t || '_' || to_char(m, 'YYYY_MM');
      IF to_regclass(format('ulduar.%I', part)) IS NULL THEN
        BEGIN
          EXECUTE format('CREATE TABLE ulduar.%I PARTITION OF ulduar.%I FOR VALUES FROM (%L) TO (%L)',
                         part, t, m AT TIME ZONE 'UTC', (m + interval '1 month') AT TIME ZONE 'UTC');
          EXECUTE format('CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.%I FOR EACH STATEMENT'
                         ' EXECUTE FUNCTION ulduar.refuse_history_change()', part);
          made := made + 1;
        EXCEPTION WHEN duplicate_table THEN NULL;
        END;
      END IF;
    END LOOP;
  END LOOP;
  RETURN made;
END
$$;

-- Drops the empty monthly partitions that ended on or before the given time; a partition holding any row stays.
-- The ledger role only (retention). The four parents are locked first, which also locks every partition: no
-- insert can land in a partition being dropped. The order is lineage_edge, artifact, archive_check,
-- archive_finding, the order in which ensure_partitions takes those four; every path takes them in that order, or touches only one
-- of them in a transaction (a recorder's other inserts; the dry run of retention counts each history in a
-- transaction of its own).
CREATE OR REPLACE FUNCTION ulduar.drop_empty_partitions(before timestamptz) RETURNS integer
  LANGUAGE plpgsql SET search_path = pg_catalog, ulduar SET TimeZone = 'UTC' SET DateStyle = 'ISO' AS
$$
DECLARE p record; month_end timestamptz; has_rows boolean; dropped integer := 0;
BEGIN
  LOCK TABLE ulduar.lineage_edge, ulduar.artifact, ulduar.archive_check, ulduar.archive_finding IN ACCESS EXCLUSIVE MODE;
  FOR p IN
    SELECT c.relname::text AS part, parent.relname::text AS tbl
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class parent ON parent.oid = i.inhparent
    WHERE i.inhparent IN ('ulduar.artifact'::regclass, 'ulduar.lineage_edge'::regclass,
                          'ulduar.archive_check'::regclass, 'ulduar.archive_finding'::regclass)
      AND c.relname::text ~ ('^' || parent.relname::text || '_[0-9]{4}_(0[1-9]|1[0-2])$')
    ORDER BY c.relname
  LOOP
    month_end := (to_date(right(p.part, 7), 'YYYY_MM') + interval '1 month') AT TIME ZONE 'UTC';
    IF month_end <= before THEN
      EXECUTE format('SELECT EXISTS (SELECT FROM ulduar.%I)', p.part) INTO has_rows;
      IF NOT has_rows THEN
        EXECUTE format('DROP TABLE ulduar.%I', p.part);
        dropped := dropped + 1;
      END IF;
    END IF;
  END LOOP;
  RETURN dropped;
END
$$;

-- Isolation between projects (D29), as step 1.
ALTER TABLE ulduar.archive_registration ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_registration FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_retention_rule ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_retention_rule FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_deletion ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_deletion FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_check ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_check FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_finding ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.archive_finding FORCE ROW LEVEL SECURITY;

CREATE POLICY ledger_all ON ulduar.archive_registration TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.archive_retention_rule TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.archive_deletion TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.archive_check TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.archive_finding TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());

-- A recording role reads and inserts only its own project's rows; a retention rule, a redaction and a history-retention
-- action it can only read.
CREATE POLICY member_select ON ulduar.archive_registration FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.archive_registration FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.archive_deletion FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.archive_deletion FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.archive_check FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.archive_check FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.archive_finding FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.archive_finding FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.archive_retention_rule FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
-- Step 1's redaction and history-retention action records, readable by the recording role so it can copy them to the
-- archive store (D32, H4): they hold ids, field names, reasons, counts and times, never a removed value.
CREATE POLICY member_select ON ulduar.redaction FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.history_retention_action FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));

-- Grants: nothing beyond select and insert (a rule: select only).
GRANT SELECT, INSERT ON ulduar.archive_registration, ulduar.archive_deletion, ulduar.archive_check,
  ulduar.archive_finding TO ulduar_recorder;
GRANT SELECT ON ulduar.archive_retention_rule, ulduar.redaction, ulduar.history_retention_action TO ulduar_recorder;
REVOKE ALL ON FUNCTION ulduar.artifact_is_archived(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ulduar.artifact_is_archived(uuid, uuid) TO ulduar_recorder;

SELECT ulduar.ensure_partitions(now());
