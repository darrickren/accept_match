-- AcceptMatch MVP P0 精简表结构（tasks.md "P0 的 6 张表"）。
-- 所有业务数据都是合成数据；synthetic 列固定为 1。
-- 重置时清空除 audit_events 以外的表；audit_events 只追加，跨演示轮次保留。


-- 单行：当前演示轮次、参数组、种子哈希和检查预算
CREATE TABLE IF NOT EXISTS meta (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    epoch                  INTEGER NOT NULL CHECK (epoch >= 1),
    parameter_set          TEXT    NOT NULL,
    seed_hash              TEXT    NOT NULL,
    budget_initial_cents   INTEGER NOT NULL CHECK (budget_initial_cents >= 0),
    budget_remaining_cents INTEGER NOT NULL CHECK (budget_remaining_cents >= 0),
    budget_version         INTEGER NOT NULL CHECK (budget_version >= 0),
    reset_at               TEXT    NOT NULL,
    CHECK (budget_remaining_cents <= budget_initial_cents)
);

CREATE TABLE IF NOT EXISTS items (
    item_id           TEXT    PRIMARY KEY,
    product_id        TEXT    NOT NULL,
    tag_code          TEXT    NOT NULL UNIQUE,
    item_status       TEXT    NOT NULL CHECK (item_status IN ('INTAKE', 'AVAILABLE', 'RESERVED', 'DELIVERED')),
    condition_version INTEGER NOT NULL CHECK (condition_version >= 1),
    synthetic         INTEGER NOT NULL CHECK (synthetic = 1)
);

-- 不可变的 Condition_Record 快照；points_json 含各部位的状态、缺陷和 Evidence
CREATE TABLE IF NOT EXISTS condition_versions (
    item_id     TEXT    NOT NULL REFERENCES items (item_id),
    version     INTEGER NOT NULL CHECK (version >= 1),
    points_json TEXT    NOT NULL CHECK (json_valid(points_json)),
    source_ref  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    synthetic   INTEGER NOT NULL CHECK (synthetic = 1),
    PRIMARY KEY (item_id, version)
);

CREATE TABLE IF NOT EXISTS buyer_requests (
    request_id               TEXT PRIMARY KEY,
    buyer_id                 TEXT NOT NULL,
    product_id               TEXT NOT NULL,
    accepted_defects_json    TEXT NOT NULL CHECK (json_valid(accepted_defects_json)),
    required_conditions_json TEXT NOT NULL CHECK (json_valid(required_conditions_json)),
    status                   TEXT NOT NULL CHECK (status IN (
        'SUBMITTED', 'EVALUATED', 'WAITING_FOR_INSPECTION', 'INSPECTION_RESULT_RECORDED',
        'EVIDENCE_READY', 'OFFERED', 'NEEDS_REEVALUATION', 'RESERVED', 'DELIVERED',
        'DECLINED', 'NO_MATCH')),
    exclusions_json          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(exclusions_json)),
    no_match_json            TEXT CHECK (no_match_json IS NULL OR json_valid(no_match_json)),
    created_at               TEXT NOT NULL,
    synthetic                INTEGER NOT NULL CHECK (synthetic = 1)
);

-- 匹配评估、经济判断、检查任务、证据包、报价、预留
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id       TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN (
        'EVALUATION', 'DECISION', 'INSPECTION_TASK', 'EVIDENCE_PACKAGE', 'QUOTE', 'RESERVATION')),
    request_id        TEXT REFERENCES buyer_requests (request_id),
    item_id           TEXT REFERENCES items (item_id),
    condition_version INTEGER CHECK (condition_version IS NULL OR condition_version >= 1),
    status            TEXT NOT NULL,
    natural_key       TEXT NOT NULL,
    content_json      TEXT NOT NULL CHECK (json_valid(content_json)),
    created_at        TEXT NOT NULL,
    synthetic         INTEGER NOT NULL CHECK (synthetic = 1),
    UNIQUE (kind, natural_key),
    CHECK (
        (kind = 'RESERVATION'     AND status IN ('ACTIVE', 'FULFILLED')) OR
        (kind = 'INSPECTION_TASK' AND status IN ('OPEN', 'COMPLETED')) OR
        (kind = 'QUOTE'           AND status IN ('ACTIVE', 'SUPERSEDED', 'ACCEPTED', 'DECLINED')) OR
        (kind IN ('EVALUATION', 'DECISION', 'EVIDENCE_PACKAGE') AND status = 'RECORDED')
    ),
    CHECK (kind = 'EVALUATION' OR item_id IS NOT NULL)
);

-- 一件商品同一时间最多一个有效预留（需求 10.6）
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifacts_active_reservation
    ON artifacts (item_id) WHERE kind = 'RESERVATION' AND status = 'ACTIVE';

-- 一件商品同一时间最多一个未完成检查任务（P0 简化：按商品而非按部位）
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifacts_open_inspection_task
    ON artifacts (item_id) WHERE kind = 'INSPECTION_TASK' AND status = 'OPEN';

CREATE INDEX IF NOT EXISTS ix_artifacts_request ON artifacts (request_id, kind);

-- 只追加的操作记录，跨演示轮次保留（不设外键，重置后仍可查询）
CREATE TABLE IF NOT EXISTS audit_events (
    epoch            INTEGER NOT NULL CHECK (epoch >= 0),
    seq              INTEGER NOT NULL CHECK (seq >= 1),
    event_id         TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    actor_type       TEXT    NOT NULL CHECK (actor_type IN (
        'operator', 'buyer', 'inspector', 'delivery', 'agent', 'system')),
    actor_id         TEXT    NOT NULL,
    event_type       TEXT    NOT NULL,
    item_id          TEXT,
    request_id       TEXT,
    related_ids_json TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(related_ids_json)),
    payload_json     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    outcome          TEXT    NOT NULL,
    PRIMARY KEY (epoch, seq),
    UNIQUE (epoch, event_id)
);

CREATE INDEX IF NOT EXISTS ix_audit_item ON audit_events (item_id, epoch, seq);
CREATE INDEX IF NOT EXISTS ix_audit_request ON audit_events (request_id, epoch, seq);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS condition_versions_no_update BEFORE UPDATE ON condition_versions
BEGIN
    SELECT RAISE(ABORT, 'condition_versions is immutable');
END;

-- 最后设置版本号：只有所有表都创建成功后才标记为已初始化
PRAGMA user_version = 1;
