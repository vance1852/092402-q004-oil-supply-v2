"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_usd TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES price_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(price_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON price_index_quotes(price_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS price_dispute_settings (
    price_index TEXT PRIMARY KEY,
    tolerance_usd TEXT NOT NULL,
    updated_by TEXT NOT NULL REFERENCES supply_users(user_id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    tolerance_usd TEXT NOT NULL,
    spread_usd TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','decided','returned')),
    opened_by TEXT,
    opened_at TEXT NOT NULL,
    decided_at TEXT,
    decision_reason TEXT,
    UNIQUE(price_index, trade_date, round_no)
);

CREATE INDEX IF NOT EXISTS idx_disputes_state
ON price_disputes(state, price_index, trade_date);

CREATE TABLE IF NOT EXISTS price_dispute_candidates (
    dispute_id INTEGER NOT NULL REFERENCES price_disputes(dispute_id),
    quote_id INTEGER NOT NULL REFERENCES price_index_quotes(quote_id),
    added_at TEXT NOT NULL,
    PRIMARY KEY(dispute_id, quote_id)
);

CREATE TABLE IF NOT EXISTS price_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    dispute_id INTEGER REFERENCES price_disputes(dispute_id),
    close_usd TEXT NOT NULL,
    basis TEXT NOT NULL CHECK(basis IN ('auto_matched','candidate_selected','adjudicated')),
    source_quote_id INTEGER REFERENCES price_index_quotes(quote_id),
    consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
    decided_by TEXT REFERENCES supply_users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE(price_index, trade_date, round_no)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_series
ON price_confirmations(price_index, trade_date, confirmation_id);

CREATE TRIGGER IF NOT EXISTS trg_confirmation_lock_update
BEFORE UPDATE ON price_confirmations
WHEN OLD.consumed=1 AND (
    NEW.price_index IS NOT OLD.price_index
    OR NEW.trade_date IS NOT OLD.trade_date
    OR NEW.round_no IS NOT OLD.round_no
    OR NEW.dispute_id IS NOT OLD.dispute_id
    OR NEW.close_usd IS NOT OLD.close_usd
    OR NEW.basis IS NOT OLD.basis
    OR NEW.source_quote_id IS NOT OLD.source_quote_id
    OR NEW.decided_by IS NOT OLD.decided_by
    OR NEW.decided_at IS NOT OLD.decided_at
    OR NEW.consumed=0
)
BEGIN
    SELECT RAISE(ABORT, '已使用的复核结论不可覆盖');
END;

CREATE TRIGGER IF NOT EXISTS trg_confirmation_lock_delete
BEFORE DELETE ON price_confirmations
WHEN OLD.consumed=1
BEGIN
    SELECT RAISE(ABORT, '已使用的复核结论不可删除');
END;

CREATE TABLE IF NOT EXISTS price_valuation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    confirmation_id INTEGER NOT NULL REFERENCES price_confirmations(confirmation_id),
    close_usd TEXT NOT NULL,
    positions_json TEXT NOT NULL,
    valuation_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_barrels TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_barrels TEXT NOT NULL,
    available_barrels TEXT NOT NULL,
    unit_cost_usd TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_barrels TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_barrels TEXT NOT NULL,
    allocated_barrels TEXT NOT NULL DEFAULT '0',
    delivered_barrels TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_barrels TEXT NOT NULL,
    expected_delivered_barrels TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
