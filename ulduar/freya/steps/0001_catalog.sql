-- ulduar-kind: additive
-- ulduar-min-library: 0.1.0
-- Step 1, catalog: projects, scopes, datasets, artifacts, lineage, history retention and redaction.
-- Runs as the ledger role (the owner of schema ulduar), inside the ledger's transaction.

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ulduar_recorder') THEN
    CREATE ROLE ulduar_recorder NOLOGIN;
  END IF;
END $$;

-- True for the owner of schema ulduar and the roles that hold it: the one test every ledger-only policy uses.
CREATE FUNCTION ulduar.is_ledger_role() RETURNS boolean LANGUAGE sql STABLE
  SET search_path = pg_catalog AS
$$ SELECT pg_has_role(current_user, (SELECT nspowner FROM pg_namespace WHERE nspname = 'ulduar'), 'USAGE') $$;

-- Projects, membership, scopes: written by the ledger role only (onboarding).
CREATE TABLE ulduar.project (
  project_id  uuid PRIMARY KEY,
  name        text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ulduar.project_member (
  role_name   name PRIMARY KEY,                      -- one login role belongs to one project
  project_id  uuid NOT NULL REFERENCES ulduar.project,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ulduar.scope (
  project_id  uuid NOT NULL REFERENCES ulduar.project,
  scope       text NOT NULL CHECK (scope ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),  -- one level in v0.1 (D27)
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, scope)
);
CREATE FUNCTION ulduar.member_projects() RETURNS SETOF uuid LANGUAGE sql STABLE
  SET search_path = pg_catalog, ulduar AS
$$ SELECT project_id FROM ulduar.project_member WHERE role_name = current_user $$;

-- Catalog.
CREATE TABLE ulduar.artifact_kind (kind text PRIMARY KEY);
INSERT INTO ulduar.artifact_kind VALUES ('raw_file'), ('database_dump'), ('report'), ('model'), ('log');

CREATE TABLE ulduar.dataset (
  project_id  uuid NOT NULL,
  scope       text NOT NULL,
  dataset_id  uuid NOT NULL CHECK (uuid_extract_version(dataset_id) = 7),
  namespace   text NOT NULL CHECK (length(namespace) BETWEEN 1 AND 200),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 500),
  event_at    timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(dataset_id)),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, dataset_id),
  UNIQUE (project_id, scope, namespace, name),
  UNIQUE (project_id, scope, dataset_id),             -- the target of the artifact's scope-bound reference
  FOREIGN KEY (project_id, scope) REFERENCES ulduar.scope
);

CREATE TABLE ulduar.artifact (
  project_id         uuid NOT NULL,
  scope              text NOT NULL,
  artifact_id        uuid NOT NULL CHECK (uuid_extract_version(artifact_id) = 7),
  event_at           timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(artifact_id)),
  recorded_at        timestamptz NOT NULL DEFAULT now(),
  kind               text NOT NULL REFERENCES ulduar.artifact_kind,
  checksum_algorithm text NOT NULL CHECK (checksum_algorithm = 'sha256'),
  checksum           bytea NOT NULL CHECK (octet_length(checksum) = 32),
  size_bytes         bigint NOT NULL CHECK (size_bytes >= 0),
  origin             text CHECK (length(origin) BETWEEN 1 AND 1000),  -- a path or id within the source, never an actor
  dataset_id         uuid,
  PRIMARY KEY (project_id, artifact_id, event_at),
  FOREIGN KEY (project_id, scope) REFERENCES ulduar.scope,
  FOREIGN KEY (project_id, scope, dataset_id) REFERENCES ulduar.dataset (project_id, scope, dataset_id)  -- the dataset is in the artifact's scope
) PARTITION BY RANGE (event_at);
CREATE INDEX ON ulduar.artifact (project_id, artifact_id);
CREATE INDEX ON ulduar.artifact (project_id, scope, checksum);

-- Lineage: child "derived from" parent; nodes are artifacts or datasets.
CREATE TABLE ulduar.lineage_edge (
  project_id  uuid NOT NULL,
  edge_id     uuid NOT NULL CHECK (uuid_extract_version(edge_id) = 7),
  event_at    timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(edge_id)),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  child_kind  text NOT NULL CHECK (child_kind IN ('artifact', 'dataset')),
  child_id    uuid NOT NULL,
  parent_kind text NOT NULL CHECK (parent_kind IN ('artifact', 'dataset')),
  parent_id   uuid NOT NULL,
  run_id      uuid,                                   -- Thorim's run, from Phase 4
  CHECK (child_id <> parent_id),
  PRIMARY KEY (project_id, edge_id, event_at)
) PARTITION BY RANGE (event_at);
CREATE INDEX ON ulduar.lineage_edge (project_id, child_id);
CREATE INDEX ON ulduar.lineage_edge (project_id, parent_id);

CREATE FUNCTION ulduar.node_exists(p uuid, k text, n uuid) RETURNS boolean LANGUAGE sql STABLE
  SET search_path = pg_catalog, ulduar AS
$$ SELECT CASE k WHEN 'artifact' THEN EXISTS (SELECT FROM ulduar.artifact WHERE project_id = p AND artifact_id = n)
                 ELSE EXISTS (SELECT FROM ulduar.dataset WHERE project_id = p AND dataset_id = n) END $$;

-- One lock per project serialises edge inserts, so two concurrent edges cannot close a cycle together; the
-- check reads the edges the lock holder committed, which only a READ COMMITTED transaction sees (each
-- statement takes a new snapshot), so any other isolation level is refused.
-- UL001: the edge would close a cycle. UL002: a node is not in the project. UL003: not READ COMMITTED.
CREATE FUNCTION ulduar.check_lineage_edge() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, ulduar AS
$$
BEGIN
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'ulduar: lineage edges need a READ COMMITTED transaction, this one is %',
      current_setting('transaction_isolation') USING ERRCODE = 'UL003';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('ulduar.lineage:' || NEW.project_id::text, 0));
  IF NOT ulduar.node_exists(NEW.project_id, NEW.child_kind, NEW.child_id)
     OR NOT ulduar.node_exists(NEW.project_id, NEW.parent_kind, NEW.parent_id) THEN
    RAISE EXCEPTION 'ulduar: lineage edge % names a node that is not in its project', NEW.edge_id
      USING ERRCODE = 'UL002';
  END IF;
  IF EXISTS (
    WITH RECURSIVE up(id) AS (
      SELECT NEW.parent_id
      UNION
      SELECT e.parent_id FROM ulduar.lineage_edge e JOIN up ON e.child_id = up.id WHERE e.project_id = NEW.project_id
    ) SELECT FROM up WHERE id = NEW.child_id
  ) THEN
    RAISE EXCEPTION 'ulduar: lineage edge % would close a cycle', NEW.edge_id USING ERRCODE = 'UL001';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER check_edge BEFORE INSERT ON ulduar.lineage_edge FOR EACH ROW EXECUTE FUNCTION ulduar.check_lineage_edge();

-- History retention and redaction (D40): records that never age out and are never redacted.
CREATE TABLE ulduar.history_retention_rule (
  project_id  uuid NOT NULL REFERENCES ulduar.project,
  rule_id     uuid NOT NULL CHECK (uuid_extract_version(rule_id) = 7),
  event_at    timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(rule_id)),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  history     text NOT NULL CHECK (history IN ('artifact', 'lineage_edge')),
  keep_days   integer CHECK (keep_days > 0),          -- NULL: keep for ever (the default when no rule exists)
  PRIMARY KEY (project_id, rule_id)
);
CREATE TABLE ulduar.history_retention_action (
  project_id   uuid NOT NULL REFERENCES ulduar.project,
  action_id    uuid NOT NULL CHECK (uuid_extract_version(action_id) = 7),
  event_at     timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(action_id)),
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  rule_id      uuid NOT NULL,
  history      text NOT NULL,
  cutoff       timestamptz NOT NULL,
  rows_removed bigint NOT NULL CHECK (rows_removed >= 0),
  PRIMARY KEY (project_id, action_id),
  FOREIGN KEY (project_id, rule_id) REFERENCES ulduar.history_retention_rule
);
CREATE TABLE ulduar.redaction (
  project_id   uuid NOT NULL REFERENCES ulduar.project,
  redaction_id uuid NOT NULL CHECK (uuid_extract_version(redaction_id) = 7),
  event_at     timestamptz NOT NULL CHECK (event_at = uuid_extract_timestamp(redaction_id)),
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  history      text NOT NULL,
  record_id    uuid NOT NULL,
  field        text NOT NULL CHECK ((history, field) IN (('artifact', 'origin'), ('dataset', 'name'), ('dataset', 'namespace'))),
  reason       text NOT NULL CHECK (reason IN ('personal_data', 'secret', 'recorded_by_mistake')),
  PRIMARY KEY (project_id, redaction_id)
);

-- recorded_at is the database's time, taken when the row is inserted (statement time, not the transaction's
-- start): whatever an inserting role supplies is overwritten (the DEFAULT stays for documentation; the trigger
-- is what makes it true).
CREATE FUNCTION ulduar.stamp_recorded_at() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog AS
$$ BEGIN NEW.recorded_at := clock_timestamp(); RETURN NEW; END $$;
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.project FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.project_member FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.scope FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.dataset FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.artifact FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.lineage_edge FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.history_retention_rule FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.history_retention_action FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();
CREATE TRIGGER stamp_recorded_at BEFORE INSERT ON ulduar.redaction FOR EACH ROW EXECUTE FUNCTION ulduar.stamp_recorded_at();

-- Append-only, enforced by the database (D30): only the ledger role, in a declared maintenance mode.
-- Each trigger argument is `<operation>:<mode>` and lists one change that may pass in that mode (the setting
-- `ulduar.maintenance`), so a mode never allows another operation; no argument means never.
CREATE FUNCTION ulduar.refuse_history_change() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, ulduar AS
$$
BEGIN
  IF ulduar.is_ledger_role()
     AND TG_OP || ':' || coalesce(current_setting('ulduar.maintenance', true), '') = ANY (TG_ARGV) THEN
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD WHEN 'UPDATE' THEN NEW END;
  END IF;
  RAISE EXCEPTION 'ulduar: % on %.% refused: recorded history is append-only', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.artifact FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change('DELETE:retention', 'UPDATE:redaction');
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.lineage_edge FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change('DELETE:retention');
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.dataset FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change('UPDATE:redaction');
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.history_retention_rule FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.history_retention_action FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON ulduar.redaction FOR EACH ROW EXECUTE FUNCTION ulduar.refuse_history_change();
-- A truncate is refused unconditionally: no argument means no maintenance mode passes. Statement triggers are
-- not inherited by partitions, so ensure_partitions attaches the same trigger to every partition it makes.
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.artifact FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.lineage_edge FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.dataset FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.history_retention_rule FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.history_retention_action FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();
CREATE TRIGGER refuse_truncate BEFORE TRUNCATE ON ulduar.redaction FOR EACH STATEMENT EXECUTE FUNCTION ulduar.refuse_history_change();

-- Time partitions: created only by this step-defined function (R7); monthly, UTC.
-- Callers serialise on the advisory lock -6595867142526573570 (the first 8 bytes of SHA-256("ulduar.partitions")
-- as a big-endian signed 64-bit integer), and a partition made meanwhile is not an error (duplicate_table), so
-- concurrent callers all succeed; the result counts only the partitions this call made. for_time is bounded to
-- [now() - 15 years, now() + 2 years], which keeps every partition name's year at four digits; outside it the
-- call raises. TimeZone and DateStyle are pinned so the bounds never depend on the caller's session.
-- Each partition gets the refuse_truncate trigger (the parent's is not inherited). lineage_edge is done before
-- artifact: CREATE ... PARTITION OF locks the parent, and that is the lock order of an edge recorder. Creating
-- an artifact partition also takes SHARE ROW EXCLUSIVE on dataset, scope and artifact_kind, through the cloned
-- foreign keys. The lock order of every path is lineage_edge, artifact, then {dataset, scope, artifact_kind}.
CREATE FUNCTION ulduar.ensure_partitions(for_time timestamptz, months_ahead integer DEFAULT 3) RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, ulduar SET TimeZone = 'UTC' SET DateStyle = 'ISO' AS
$$
DECLARE m timestamp; t text; part text; made integer := 0;
BEGIN
  IF months_ahead NOT BETWEEN 0 AND 24 THEN RAISE EXCEPTION 'ulduar: months_ahead out of range'; END IF;
  IF for_time IS NULL OR for_time NOT BETWEEN now() - interval '15 years' AND now() + interval '2 years' THEN
    RAISE EXCEPTION 'ulduar: partition time % is outside the window from 15 years ago to 2 years ahead', for_time;
  END IF;
  PERFORM pg_advisory_xact_lock(-6595867142526573570);
  FOREACH t IN ARRAY ARRAY['lineage_edge', 'artifact'] LOOP
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
-- The ledger role only (retention). Both parents are locked first, which also locks every partition: no insert
-- can land in a partition being dropped. The order is lineage_edge, then artifact, the order of the only recorder
-- that holds both (an edge insert locks lineage_edge, then its trigger reads artifact). The rule that keeps these
-- paths from deadlocking: every path takes lineage_edge, artifact, {dataset, scope, artifact_kind} in that order,
-- or touches only one of them in a transaction (a recorder's other inserts; the dry run of retention counts each
-- history in a transaction of its own).
CREATE FUNCTION ulduar.drop_empty_partitions(before timestamptz) RETURNS integer
  LANGUAGE plpgsql SET search_path = pg_catalog, ulduar SET TimeZone = 'UTC' SET DateStyle = 'ISO' AS
$$
DECLARE p record; month_end timestamptz; has_rows boolean; dropped integer := 0;
BEGIN
  LOCK TABLE ulduar.lineage_edge, ulduar.artifact IN ACCESS EXCLUSIVE MODE;
  FOR p IN
    SELECT c.relname::text AS part, parent.relname::text AS tbl
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class parent ON parent.oid = i.inhparent
    WHERE i.inhparent IN ('ulduar.artifact'::regclass, 'ulduar.lineage_edge'::regclass)
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

-- Isolation between projects (D29): row-level security forced on every keyed table, keyed on project_member.
ALTER TABLE ulduar.project ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.project FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.project_member ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.project_member FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.scope ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.scope FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.dataset ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.dataset FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.artifact FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.lineage_edge ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.lineage_edge FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.history_retention_rule ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.history_retention_rule FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.history_retention_action ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.history_retention_action FORCE ROW LEVEL SECURITY;
ALTER TABLE ulduar.redaction ENABLE ROW LEVEL SECURITY;
ALTER TABLE ulduar.redaction FORCE ROW LEVEL SECURITY;

-- The ledger role sees and writes everything; forced RLS would otherwise lock out the table owner too.
CREATE POLICY ledger_all ON ulduar.project TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.project_member TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.scope TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.dataset TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.artifact TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.lineage_edge TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.history_retention_rule TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.history_retention_action TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());
CREATE POLICY ledger_all ON ulduar.redaction TO PUBLIC USING (ulduar.is_ledger_role()) WITH CHECK (ulduar.is_ledger_role());

-- A recording role reads and inserts only its own project's rows.
CREATE POLICY member_select ON ulduar.dataset FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.dataset FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.artifact FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.artifact FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.lineage_edge FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_insert ON ulduar.lineage_edge FOR INSERT TO ulduar_recorder WITH CHECK (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.project FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.scope FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.history_retention_rule FOR SELECT TO ulduar_recorder USING (project_id IN (SELECT ulduar.member_projects()));
CREATE POLICY member_select ON ulduar.project_member FOR SELECT TO ulduar_recorder USING (role_name = current_user);

-- Grants: nothing beyond select and insert; no update, delete, truncate, references or trigger.
GRANT USAGE ON SCHEMA ulduar TO ulduar_recorder;
GRANT SELECT, INSERT ON ulduar.dataset, ulduar.artifact, ulduar.lineage_edge TO ulduar_recorder;
GRANT SELECT ON ulduar.project, ulduar.project_member, ulduar.scope, ulduar.artifact_kind,
  ulduar.history_retention_rule, ulduar.schema_ledger TO ulduar_recorder;
REVOKE ALL ON FUNCTION ulduar.member_projects() FROM PUBLIC;
REVOKE ALL ON FUNCTION ulduar.node_exists(uuid, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION ulduar.is_ledger_role() FROM PUBLIC;
REVOKE ALL ON FUNCTION ulduar.ensure_partitions(timestamptz, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ulduar.drop_empty_partitions(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ulduar.member_projects() TO ulduar_recorder;
GRANT EXECUTE ON FUNCTION ulduar.node_exists(uuid, text, uuid) TO ulduar_recorder;
GRANT EXECUTE ON FUNCTION ulduar.is_ledger_role() TO ulduar_recorder;
GRANT EXECUTE ON FUNCTION ulduar.ensure_partitions(timestamptz, integer) TO ulduar_recorder;

SELECT ulduar.ensure_partitions(now());
