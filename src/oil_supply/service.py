"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    CRUDE_GRADES,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    decimal_value,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .risk import mark_to_market
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "report.read"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "quote.review", "quote.config", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

DEFAULT_TOLERANCE_USD = Decimal("0.50")


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def _tolerance(self, price_index: str) -> Decimal:
        row = self.connection.execute(
            "SELECT tolerance_usd FROM price_dispute_settings WHERE price_index=?",
            (price_index,),
        ).fetchone()
        return DEFAULT_TOLERANCE_USD if row is None else Decimal(row["tolerance_usd"])

    def configure_price_tolerance(self, actor_id: str, price_index: str, tolerance_usd: object) -> dict[str, Any]:
        self._require(actor_id, "quote.config")
        tolerance = decimal_value(tolerance_usd, "tolerance_usd", minimum=Decimal("0"))
        index = required_text(price_index, "price_index", 16).upper()
        if index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("price_index 必须是 BRENT、WTI、DUBAI、ESPO 或 URAL")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO price_dispute_settings(price_index,tolerance_usd,updated_by,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(price_index) DO UPDATE SET "
                "tolerance_usd=excluded.tolerance_usd,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (index, decimal_text(tolerance), actor_id, self._now()),
            )
            self._audit("price_index", index, "price.tolerance_configured", actor_id, {"tolerance_usd": decimal_text(tolerance)})
        return {"price_index": index, "tolerance_usd": decimal_text(tolerance)}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        tolerance = self._tolerance(quote.price_index)
        result: dict[str, Any]
        try:
            with transaction(self.connection, immediate=True):
                duplicate = self.connection.execute(
                    "SELECT quote_id FROM price_index_quotes WHERE price_index=? AND trade_date=? AND source_revision=?",
                    (quote.price_index, quote.trade_date, quote.source_revision),
                ).fetchone()
                if duplicate is not None:
                    raise Conflict("同一来源修订已登记")
                prior = self.connection.execute(
                    "SELECT quote_id FROM price_index_quotes WHERE price_index=? AND trade_date=? "
                    "ORDER BY quote_id DESC LIMIT 1",
                    (quote.price_index, quote.trade_date),
                ).fetchone()
                cursor = self.connection.execute(
                    "INSERT INTO price_index_quotes(price_index,trade_date,close_usd,source_revision,observed_at,"
                    "supersedes_quote_id,round,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        quote.price_index,
                        quote.trade_date,
                        decimal_text(quote.close_usd),
                        quote.source_revision,
                        quote.observed_at,
                        None if prior is None else prior["quote_id"],
                        1,
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                outcome = self._classify_quote(quote_id, quote, tolerance)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"price_index": quote.price_index, "trade_date": quote.trade_date, "outcome": outcome["state"]},
                )
                if outcome.get("newly_opened"):
                    self._audit(
                        "price_dispute",
                        str(outcome["dispute_id"]),
                        "price_dispute.opened",
                        "system",
                        {"price_index": quote.price_index, "trade_date": quote.trade_date,
                         "round": outcome["round"], "max_gap_usd": outcome["max_gap_usd"]},
                    )
                result = {"quote_id": quote_id, "price_index": quote.price_index, "trade_date": quote.trade_date,
                          **{key: value for key, value in outcome.items() if key != "newly_opened"}}
        except sqlite3.IntegrityError as exc:
            raise Conflict("报价版本冲突") from exc
        return result

    def _candidate_spread(self, price_index: str, trade_date: str) -> Decimal:
        rows = self.connection.execute(
            "SELECT close_usd FROM price_index_quotes WHERE price_index=? AND trade_date=?",
            (price_index, trade_date),
        ).fetchall()
        values = [Decimal(row["close_usd"]) for row in rows]
        return max(values) - min(values)

    def _classify_quote(self, quote_id: int, quote: IndexQuote, tolerance: Decimal) -> dict[str, Any]:
        """在已持有写事务时判定候选报价：确认、容差内保留或开启争议轮次。"""
        confirmed = self.connection.execute(
            "SELECT * FROM price_confirmed_values WHERE price_index=? AND trade_date=? "
            "ORDER BY round DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        if confirmed is None:
            self.connection.execute(
                "INSERT INTO price_confirmed_values(price_index,trade_date,round,dispute_id,close_usd,basis,"
                "selected_quote_id,rationale,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    quote.price_index,
                    quote.trade_date,
                    1,
                    None,
                    decimal_text(quote.close_usd),
                    "auto_single_source",
                    quote_id,
                    "唯一授权来源候选，自动确认",
                    "system",
                    self._now(),
                ),
            )
            return {"state": "confirmed", "round": 1, "basis": "auto_single_source", "dispute_id": None}

        open_dispute = self.connection.execute(
            "SELECT * FROM price_disputes WHERE price_index=? AND trade_date=? AND state='open' "
            "ORDER BY round DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        if open_dispute is not None:
            round_no = int(open_dispute["round"])
            self.connection.execute(
                "UPDATE price_index_quotes SET round=? WHERE quote_id=?",
                (round_no, quote_id),
            )
            spread = self._candidate_spread(quote.price_index, quote.trade_date)
            self.connection.execute(
                "UPDATE price_disputes SET max_gap_usd=? WHERE dispute_id=?",
                (decimal_text(spread), open_dispute["dispute_id"]),
            )
            return {"state": "disputed", "round": round_no, "basis": None, "dispute_id": int(open_dispute["dispute_id"])}

        latest_dispute = self.connection.execute(
            "SELECT * FROM price_disputes WHERE price_index=? AND trade_date=? ORDER BY round DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        returned_for_evidence = latest_dispute is not None and latest_dispute["state"] == "returned"

        round_no = int(confirmed["round"])
        anchor_price = Decimal(confirmed["close_usd"])
        gap = abs(quote.close_usd - anchor_price)
        if not returned_for_evidence and confirmed["state"] == "active" and gap <= tolerance:
            self.connection.execute(
                "UPDATE price_index_quotes SET round=? WHERE quote_id=?",
                (round_no, quote_id),
            )
            return {"state": "within_tolerance", "round": round_no, "basis": confirmed["basis"], "dispute_id": None}

        # 退回补证后补交证据，或差值超出容差：迟到候选只能开启新一轮争议。
        dispute_round = self.connection.execute(
            "SELECT max(round) AS round FROM price_disputes WHERE price_index=? AND trade_date=?",
            (quote.price_index, quote.trade_date),
        ).fetchone()["round"]
        new_round = max(round_no, int(dispute_round or 0)) + 1
        self.connection.execute(
            "UPDATE price_index_quotes SET round=? WHERE quote_id=?",
            (new_round, quote_id),
        )
        # 尚未被下游使用的自动确认可以撤回；已使用的结论保持有效且不可覆盖，
        # 新轮次确认后才在读取侧切换，历史快照不受影响。
        if confirmed["state"] == "active" and confirmed["consumed_at"] is None:
            self.connection.execute(
                "UPDATE price_confirmed_values SET state='superseded' WHERE confirmed_id=?",
                (confirmed["confirmed_id"],),
            )
        spread = self._candidate_spread(quote.price_index, quote.trade_date)
        cursor = self.connection.execute(
            "INSERT INTO price_disputes(price_index,trade_date,round,state,tolerance_usd,max_gap_usd,"
            "anchor_close_usd,opened_by_quote_id,opened_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                quote.price_index,
                quote.trade_date,
                new_round,
                "open",
                decimal_text(tolerance),
                decimal_text(spread),
                decimal_text(anchor_price),
                quote_id,
                self._now(),
            ),
        )
        dispute_id = int(cursor.lastrowid)
        return {"state": "disputed", "round": new_round, "basis": None, "dispute_id": dispute_id,
                "newly_opened": True, "max_gap_usd": decimal_text(spread)}

    def dispute_queue(self, actor_id: str, price_index: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        if price_index is not None:
            rows = self.connection.execute(
                "SELECT * FROM price_disputes WHERE state='open' AND price_index=? ORDER BY opened_at,dispute_id",
                (price_index.upper(),),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM price_disputes WHERE state='open' ORDER BY opened_at,dispute_id"
            ).fetchall()
        return {"disputes": [self._dispute_view(row) for row in rows]}

    def _dispute_view(self, row: sqlite3.Row) -> dict[str, Any]:
        candidates = self.connection.execute(
            "SELECT quote_id,close_usd,source_revision,observed_at,round,recorded_by,recorded_at "
            "FROM price_index_quotes WHERE price_index=? AND trade_date=? ORDER BY round,quote_id",
            (row["price_index"], row["trade_date"]),
        ).fetchall()
        latest_confirmed = self.connection.execute(
            "SELECT close_usd,basis,selected_quote_id,rationale,decided_by,decided_at "
            "FROM price_confirmed_values WHERE price_index=? AND trade_date=? AND round=? ",
            (row["price_index"], row["trade_date"], int(row["round"]) - 1),
        ).fetchone()
        decision = self.connection.execute(
            "SELECT action,selected_quote_id,close_usd,rationale,decided_by,decided_at "
            "FROM price_dispute_decisions WHERE dispute_id=? ORDER BY decision_id DESC LIMIT 1",
            (row["dispute_id"],),
        ).fetchone()
        return {
            "dispute_id": row["dispute_id"],
            "price_index": row["price_index"],
            "trade_date": row["trade_date"],
            "round": row["round"],
            "state": row["state"],
            "tolerance_usd": row["tolerance_usd"],
            "max_gap_usd": row["max_gap_usd"],
            "anchor_close_usd": row["anchor_close_usd"],
            "opened_at": row["opened_at"],
            "resolved_at": row["resolved_at"],
            "candidates": [dict(candidate) for candidate in candidates],
            "prior_confirmed": None if latest_confirmed is None else dict(latest_confirmed),
            "decision": None if decision is None else dict(decision),
        }

    def dispute(self, actor_id: str, dispute_id: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM price_disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if row is None:
            raise NotFound("争议不存在")
        return self._dispute_view(row)

    def resolve_dispute(
        self,
        actor_id: str,
        dispute_id: int,
        action: str,
        rationale: str,
        *,
        selected_quote_id: int | None = None,
        close_usd: object = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "quote.review")
        if action not in {"select_candidate", "adjudicate", "return"}:
            raise ValidationFailed("action 必须是 select_candidate、adjudicate 或 return")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValidationFailed("决定依据不能为空")
        rationale = rationale.strip()
        if len(rationale) > 1000:
            raise ValidationFailed("决定依据不能超过 1000 个字符")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM price_disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise NotFound("争议不存在")
            if row["state"] != "open":
                raise InvalidState("争议已经结束，结论不可覆盖")
            submitters = {
                item["recorded_by"]
                for item in self.connection.execute(
                    "SELECT recorded_by FROM price_index_quotes WHERE price_index=? AND trade_date=?",
                    (row["price_index"], row["trade_date"]),
                ).fetchall()
            }
            if actor_id in submitters:
                raise Forbidden("提交报价的人不能复核自己的记录")

            chosen_price: Decimal
            chosen_quote: int | None = None
            adjudicated = close_usd
            if action == "select_candidate":
                if selected_quote_id is None:
                    raise ValidationFailed("选择候选时必须提供 selected_quote_id")
                candidate = self.connection.execute(
                    "SELECT * FROM price_index_quotes WHERE quote_id=? AND price_index=? AND trade_date=?",
                    (selected_quote_id, row["price_index"], row["trade_date"]),
                ).fetchone()
                if candidate is None:
                    raise ValidationFailed("所选候选不属于该交易日")
                chosen_price = Decimal(candidate["close_usd"])
                chosen_quote = int(candidate["quote_id"])
            elif action == "adjudicate":
                if adjudicated is None:
                    raise ValidationFailed("核定决定必须提供 close_usd")
                chosen_price = decimal_value(adjudicated, "close_usd", minimum=Decimal("0.01"))
            else:
                chosen_price = Decimal(row["anchor_close_usd"]) if row["anchor_close_usd"] is not None else Decimal("0")

            cursor = self.connection.execute(
                "UPDATE price_disputes SET state=?,resolved_at=? WHERE dispute_id=? AND state='open'",
                ("returned" if action == "return" else "resolved", self._now(), dispute_id),
            )
            if cursor.rowcount != 1:
                raise Conflict("争议已被其他复核人结束")
            decision_cursor = self.connection.execute(
                "INSERT INTO price_dispute_decisions(dispute_id,action,selected_quote_id,close_usd,"
                "rationale,decided_by,decided_at) VALUES(?,?,?,?,?,?,?)",
                (
                    dispute_id,
                    action,
                    chosen_quote,
                    None if action in {"select_candidate", "return"} else decimal_text(chosen_price),
                    rationale,
                    actor_id,
                    self._now(),
                ),
            )
            decision_id = int(decision_cursor.lastrowid)
            confirmed_id: int | None = None
            if action != "return":
                # 前一轮结论的值与依据永不改写；新一轮生效后它仅退出“当前值”位置，
                # consumed_at 与消费记录保留，历史快照仍可追溯。
                self.connection.execute(
                    "UPDATE price_confirmed_values SET state='superseded' "
                    "WHERE price_index=? AND trade_date=? AND round=? AND state='active'",
                    (row["price_index"], row["trade_date"], int(row["round"]) - 1),
                )
                confirmed_cursor = self.connection.execute(
                    "INSERT INTO price_confirmed_values(price_index,trade_date,round,dispute_id,close_usd,basis,"
                    "selected_quote_id,rationale,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["price_index"],
                        row["trade_date"],
                        row["round"],
                        dispute_id,
                        decimal_text(chosen_price),
                        "candidate" if action == "select_candidate" else "adjudicated",
                        chosen_quote,
                        rationale,
                        actor_id,
                        self._now(),
                    ),
                )
                confirmed_id = int(confirmed_cursor.lastrowid)
            self._audit(
                "price_dispute",
                str(dispute_id),
                f"price_dispute.{action}",
                actor_id,
                {"decision_id": decision_id, "confirmed_id": confirmed_id},
            )
        return {
            "dispute_id": dispute_id,
            "decision_id": decision_id,
            "state": "returned" if action == "return" else "resolved",
            "confirmed_id": confirmed_id,
            "close_usd": None if action == "return" else decimal_text(chosen_price),
        }

    def price_history(self, actor_id: str, price_index: str, trade_date: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT quote_id,close_usd,source_revision,observed_at,round,recorded_by,recorded_at "
            "FROM price_index_quotes WHERE price_index=? AND trade_date=? ORDER BY quote_id",
            (price_index.upper(), trade_date),
        ).fetchall()
        if not rows:
            raise NotFound("该交易日没有候选报价")
        confirmed_rows = self.connection.execute(
            "SELECT confirmed_id,round,dispute_id,close_usd,basis,selected_quote_id,rationale,"
            "decided_by,decided_at,state,consumed_at FROM price_confirmed_values "
            "WHERE price_index=? AND trade_date=? ORDER BY round",
            (price_index.upper(), trade_date),
        ).fetchall()
        dispute_rows = self.connection.execute(
            "SELECT dispute_id,round,state,tolerance_usd,max_gap_usd,anchor_close_usd,opened_at,resolved_at "
            "FROM price_disputes WHERE price_index=? AND trade_date=? ORDER BY round",
            (price_index.upper(), trade_date),
        ).fetchall()
        decisions = self.connection.execute(
            "SELECT d.decision_id,d.dispute_id,d.action,d.selected_quote_id,d.close_usd,d.rationale,"
            "d.decided_by,d.decided_at FROM price_dispute_decisions d "
            "JOIN price_disputes p ON p.dispute_id=d.dispute_id "
            "WHERE p.price_index=? AND p.trade_date=? ORDER BY d.decision_id",
            (price_index.upper(), trade_date),
        ).fetchall()
        return {
            "price_index": price_index.upper(),
            "trade_date": trade_date,
            "candidates": [dict(row) for row in rows],
            "confirmed_values": [dict(row) for row in confirmed_rows],
            "disputes": [dict(row) for row in dispute_rows],
            "decisions": [dict(row) for row in decisions],
        }

    def _confirmed_price_row(self, price_index: str, trade_date: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM price_confirmed_values WHERE price_index=? AND state='active' AND trade_date<=? "
            "ORDER BY trade_date DESC,round DESC LIMIT 1",
            (price_index.upper(), trade_date),
        ).fetchone()

    def _mark_price_consumed(self, confirmed_id: int, consumer: str, reference_id: int) -> None:
        self.connection.execute(
            "UPDATE price_confirmed_values SET consumed_at=COALESCE(consumed_at,?) WHERE confirmed_id=?",
            (self._now(), confirmed_id),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO price_value_consumptions(confirmed_id,consumer,reference_id,consumed_at) "
            "VALUES(?,?,?,?)",
            (confirmed_id, consumer, reference_id, self._now()),
        )

    def price_summary(self, price_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT v.trade_date,v.close_usd FROM price_confirmed_values v WHERE v.price_index=? AND v.state='active' "
            "AND v.round=(SELECT max(round) FROM price_confirmed_values WHERE price_index=v.price_index "
            "AND trade_date=v.trade_date AND state='active') "
            "ORDER BY v.trade_date DESC LIMIT ?",
            (price_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_usd"])) for row in rows]
        if not points:
            raise NotFound("没有基准报价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "price_index": price_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_usd": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def valuation_snapshot(self, actor_id: str, as_of_date: str, positions: list[Mapping[str, Any]]) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        if not isinstance(positions, list) or not positions:
            raise ValidationFailed("持仓列表不能为空")
        normalized = [dict(item) for item in positions]
        with transaction(self.connection, immediate=True):
            price_rows: dict[str, sqlite3.Row] = {}
            for item in normalized:
                index = str(item.get("price_index", "")).upper()
                if index not in price_rows:
                    row = self._confirmed_price_row(index, as_of_date)
                    if row is None:
                        raise InvalidState(f"{index} 截止 {as_of_date} 没有已确认值")
                    price_rows[index] = row
            prices = {index: Decimal(row["close_usd"]) for index, row in price_rows.items()}
            result = mark_to_market(normalized, prices)
            refs = [
                {
                    "price_index": index,
                    "trade_date": row["trade_date"],
                    "confirmed_id": int(row["confirmed_id"]),
                    "round": int(row["round"]),
                    "close_usd": row["close_usd"],
                }
                for index, row in sorted(price_rows.items())
            ]
            cursor = self.connection.execute(
                "INSERT INTO valuation_snapshots(as_of_date,result_json,price_refs_json,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (as_of_date, canonical_json(result), canonical_json(refs), actor_id, self._now()),
            )
            snapshot_id = int(cursor.lastrowid)
            for row in price_rows.values():
                self._mark_price_consumed(int(row["confirmed_id"]), "valuation_snapshot", snapshot_id)
            self._audit("valuation_snapshot", str(snapshot_id), "valuation.snapshotted", actor_id, {"as_of_date": as_of_date})
        return {"snapshot_id": snapshot_id, "as_of_date": as_of_date, "price_refs": refs, **result}

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_barrels,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_barrels),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                    "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.unit_cost_usd),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("库存批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("库存批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_barrels,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_barrels),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_barrels"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_barrels"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_barrels"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可发运版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        allocated = Decimal(nomination["allocated_barrels"])
        available = Decimal(lot["available_barrels"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        if available < allocated:
            raise Conflict("库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,"
                "expected_delivered_barrels,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_barrels": decimal_text(allocated),
            "expected_delivered_barrels": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        with transaction(self.connection, immediate=True):
            price_row = self._confirmed_price_row("BRENT", as_of_date)
            if price_row is None:
                raise InvalidState("截止日期没有已确认报价")
            routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
            inventory = self.connection.execute(
                "SELECT facility_id,product,sum(CAST(available_barrels AS REAL)) available_barrels "
                "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
            ).fetchall()
            input_value = {
                "scenario_sha256": row["content_sha256"],
                "as_of_date": as_of_date,
                "price_confirmed_id": price_row["confirmed_id"],
                "price": price_row["close_usd"],
                "routes": [dict(item) for item in routes],
                "inventory": [dict(item) for item in inventory],
            }
            input_sha256 = digest(input_value)
            existing = self.connection.execute(
                "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
                (scenario_id, as_of_date, input_sha256),
            ).fetchone()
            if existing is not None:
                return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
            result = scenario_projection(
                current_price=Decimal(price_row["close_usd"]),
                price_index_drop_percent=scenario.price_index_drop_percent,
                routes=routes,
                inventory=inventory,
                route_capacity_changes=scenario.route_capacity_changes,
                demand_changes=scenario.demand_changes,
            )
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._mark_price_consumed(int(price_row["confirmed_id"]), "scenario_run", run_id)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False, "price_confirmed_id": int(price_row["confirmed_id"])}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
