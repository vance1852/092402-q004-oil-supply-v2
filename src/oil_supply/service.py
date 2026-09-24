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
    date_text,
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


DEFAULT_TOLERANCE_USD = Decimal("0.50")
DISPUTE_DECISIONS = {"select", "adjudicate", "return"}

ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "report.read", "quote.review", "valuation.write"},
    "auditor": {"report.read", "audit.read"},
}


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

    def set_tolerance(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.review")
        price_index = required_text(raw.get("price_index"), "price_index", 16).upper()
        if price_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("price_index 必须是 BRENT、WTI、DUBAI、ESPO 或 URAL")
        tolerance = decimal_value(raw.get("tolerance_usd"), "tolerance_usd", minimum=Decimal("0"))
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO price_dispute_settings(price_index,tolerance_usd,updated_by,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(price_index) DO UPDATE SET "
                "tolerance_usd=excluded.tolerance_usd,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (price_index, decimal_text(tolerance), actor_id, self._now()),
            )
            self._audit("price_setting", price_index, "tolerance.updated", actor_id, {"tolerance_usd": decimal_text(tolerance)})
        return {"price_index": price_index, "tolerance_usd": decimal_text(tolerance)}

    def get_tolerance(self, actor_id: str, price_index: str) -> dict[str, Any]:
        self._require_any(actor_id, ("quote.write", "quote.review", "report.read"))
        index = required_text(price_index, "price_index", 16).upper()
        return {"price_index": index, "tolerance_usd": decimal_text(self._tolerance(index))}

    def _require_any(self, user_id: str, permissions: Iterable[str]) -> sqlite3.Row:
        user = self._user(user_id)
        granted = ROLE_PERMISSIONS[user["role"]]
        if not any(permission in granted for permission in permissions):
            raise Forbidden(f"角色 {user['role']} 无权执行该操作")
        return user

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        with transaction(self.connection, immediate=True):
            duplicate = self.connection.execute(
                "SELECT quote_id FROM price_index_quotes WHERE price_index=? AND trade_date=? AND source_revision=?",
                (quote.price_index, quote.trade_date, quote.source_revision),
            ).fetchone()
            if duplicate is not None:
                raise Conflict("同一来源修订已登记")
            previous = self.connection.execute(
                "SELECT quote_id FROM price_index_quotes WHERE price_index=? AND trade_date=? "
                "ORDER BY quote_id DESC LIMIT 1",
                (quote.price_index, quote.trade_date),
            ).fetchone()
            try:
                cursor = self.connection.execute(
                    "INSERT INTO price_index_quotes(price_index,trade_date,close_usd,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.price_index,
                        quote.trade_date,
                        decimal_text(quote.close_usd),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("报价版本冲突") from exc
            quote_id = int(cursor.lastrowid)
            self._audit(
                "quote",
                str(quote_id),
                "quote.recorded",
                actor_id,
                {"price_index": quote.price_index, "trade_date": quote.trade_date},
            )
            effect = self._evaluate_quote_locked(quote.price_index, quote.trade_date, quote_id, actor_id)
        return {
            "quote_id": quote_id,
            "price_index": quote.price_index,
            "trade_date": quote.trade_date,
            **effect,
        }

    def _same_day_quotes(self, price_index: str, trade_date: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT quote_id,close_usd,source_revision,observed_at,recorded_by,recorded_at "
            "FROM price_index_quotes WHERE price_index=? AND trade_date=? ORDER BY quote_id",
            (price_index, trade_date),
        ).fetchall()

    def _evaluate_quote_locked(
        self, price_index: str, trade_date: str, new_quote_id: int, actor_id: str
    ) -> dict[str, Any]:
        """候选到达后的争议/确认判定，调用方已经持有写事务。"""
        open_dispute = self.connection.execute(
            "SELECT * FROM price_disputes WHERE price_index=? AND trade_date=? AND state='open' "
            "ORDER BY round_no DESC LIMIT 1",
            (price_index, trade_date),
        ).fetchone()
        if open_dispute is not None:
            self.connection.execute(
                "INSERT OR IGNORE INTO price_dispute_candidates(dispute_id,quote_id,added_at) VALUES(?,?,?)",
                (open_dispute["dispute_id"], new_quote_id, self._now()),
            )
            self._audit(
                "price_dispute", str(open_dispute["dispute_id"]), "dispute.candidate_added", actor_id,
                {"quote_id": new_quote_id},
            )
            return {"effect": "dispute_candidate_added", "dispute_id": int(open_dispute["dispute_id"])}

        tolerance = self._tolerance(price_index)
        quotes = self._same_day_quotes(price_index, trade_date)
        closes = [Decimal(row["close_usd"]) for row in quotes]
        spread = max(closes) - min(closes)
        latest_dispute = self.connection.execute(
            "SELECT round_no,state FROM price_disputes WHERE price_index=? AND trade_date=? "
            "ORDER BY round_no DESC LIMIT 1",
            (price_index, trade_date),
        ).fetchone()
        last_round = self.connection.execute(
            "SELECT max(round_no) AS round_no FROM ("
            "SELECT round_no FROM price_disputes WHERE price_index=? AND trade_date=? "
            "UNION ALL SELECT round_no FROM price_confirmations WHERE price_index=? AND trade_date=?)",
            (price_index, trade_date, price_index, trade_date),
        ).fetchone()["round_no"]
        next_round = 1 if last_round is None else int(last_round) + 1
        confirmation = self.connection.execute(
            "SELECT round_no,close_usd FROM price_confirmations WHERE price_index=? AND trade_date=? "
            "ORDER BY round_no DESC LIMIT 1",
            (price_index, trade_date),
        ).fetchone()

        if confirmation is not None:
            latest_close = Decimal(quotes[-1]["close_usd"])
            if abs(latest_close - Decimal(confirmation["close_usd"])) <= tolerance:
                return {"effect": "within_tolerance"}
            dispute_id = self._open_dispute(
                price_index, trade_date, next_round, tolerance, spread, quotes, actor_id
            )
            return {"effect": "dispute_opened", "dispute_id": dispute_id}

        if latest_dispute is not None and latest_dispute["state"] == "returned":
            dispute_id = self._open_dispute(
                price_index, trade_date, next_round, tolerance, spread, quotes, actor_id
            )
            return {"effect": "dispute_opened", "dispute_id": dispute_id, "supplements_evidence": True}

        if len(quotes) < 2:
            return {"effect": "recorded"}

        if spread > tolerance:
            dispute_id = self._open_dispute(
                price_index, trade_date, next_round, tolerance, spread, quotes, actor_id
            )
            return {"effect": "dispute_opened", "dispute_id": dispute_id}

        # 两个来源在容忍度内：取最后登记的候选作为自动确认值，先到候选仍保留在台账中。
        chosen = quotes[-1]
        self.connection.execute(
            "INSERT INTO price_confirmations(price_index,trade_date,round_no,dispute_id,close_usd,basis,"
            "source_quote_id,decided_by,decided_at) VALUES(?,?,?,NULL,?,'auto_matched',?,NULL,?)",
            (
                price_index,
                trade_date,
                next_round,
                chosen["close_usd"],
                chosen["quote_id"],
                self._now(),
            ),
        )
        self._audit(
            "price_confirmation",
            f"{price_index}:{trade_date}:{next_round}",
            "price.auto_confirmed",
            actor_id,
            {"round_no": next_round, "source_quote_id": chosen["quote_id"], "spread_usd": decimal_text(spread)},
        )
        return {"effect": "auto_confirmed", "round_no": next_round}

    def _open_dispute(
        self,
        price_index: str,
        trade_date: str,
        round_no: int,
        tolerance: Decimal,
        spread: Decimal,
        quotes: Iterable[sqlite3.Row],
        actor_id: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO price_disputes(price_index,trade_date,round_no,tolerance_usd,spread_usd,state,"
            "opened_by,opened_at) VALUES(?,?,?,?,?,'open',?,?)",
            (
                price_index,
                trade_date,
                round_no,
                decimal_text(tolerance),
                decimal_text(spread),
                actor_id,
                self._now(),
            ),
        )
        dispute_id = int(cursor.lastrowid)
        quote_ids = [int(row["quote_id"]) for row in quotes]
        self.connection.executemany(
            "INSERT INTO price_dispute_candidates(dispute_id,quote_id,added_at) VALUES(?,?,?)",
            [(dispute_id, quote_id, self._now()) for quote_id in quote_ids],
        )
        self._audit(
            "price_dispute",
            str(dispute_id),
            "dispute.opened",
            actor_id,
            {
                "price_index": price_index,
                "trade_date": trade_date,
                "round_no": round_no,
                "tolerance_usd": decimal_text(tolerance),
                "spread_usd": decimal_text(spread),
                "candidate_quote_ids": quote_ids,
            },
        )
        return dispute_id

    def dispute_queue(self, actor_id: str) -> dict[str, Any]:
        self._require_any(actor_id, ("quote.write", "quote.review", "report.read"))
        rows = self.connection.execute(
            "SELECT * FROM price_disputes WHERE state='open' ORDER BY opened_at,dispute_id"
        ).fetchall()
        return {"disputes": [self._dispute_list_item(row) for row in rows]}

    def dispute_history(
        self, actor_id: str, price_index: str | None = None, trade_date: str | None = None
    ) -> dict[str, Any]:
        self._require_any(actor_id, ("quote.write", "quote.review", "report.read"))
        clauses: list[str] = []
        params: list[object] = []
        if price_index:
            index = required_text(price_index, "price_index", 16).upper()
            clauses.append("d.price_index=?")
            params.append(index)
        if trade_date:
            day = date_text(trade_date, "trade_date")
            clauses.append("d.trade_date=?")
            params.append(day)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT d.*,c.close_usd AS confirmed_close_usd,c.basis AS decision_basis,c.decided_by "
            "AS confirmation_decided_by FROM price_disputes d "
            "LEFT JOIN price_confirmations c ON c.dispute_id=d.dispute_id"
            + where
            + " ORDER BY d.dispute_id DESC",
            params,
        ).fetchall()
        return {"disputes": [self._dispute_list_item(row) for row in rows]}

    def _dispute_list_item(self, row: sqlite3.Row) -> dict[str, Any]:
        candidates = self.connection.execute(
            "SELECT q.quote_id,q.close_usd,q.source_revision,q.observed_at,q.recorded_by,q.recorded_at "
            "FROM price_dispute_candidates pc JOIN price_index_quotes q ON q.quote_id=pc.quote_id "
            "WHERE pc.dispute_id=? ORDER BY q.quote_id",
            (row["dispute_id"],),
        ).fetchall()
        item = {
            "dispute_id": int(row["dispute_id"]),
            "price_index": row["price_index"],
            "trade_date": row["trade_date"],
            "round_no": int(row["round_no"]),
            "state": row["state"],
            "tolerance_usd": row["tolerance_usd"],
            "spread_usd": row["spread_usd"],
            "opened_at": row["opened_at"],
            "decided_at": row["decided_at"],
            "candidate_count": len(candidates),
            "candidates": [dict(candidate) for candidate in candidates],
        }
        if "decision_reason" in row.keys() and row["decision_reason"] is not None:
            item["decision_reason"] = row["decision_reason"]
        confirmed_key = "confirmed_close_usd"
        if confirmed_key in row.keys() and row[confirmed_key] is not None:
            item["decision"] = {
                "basis": row["decision_basis"],
                "close_usd": row["confirmed_close_usd"],
                "decided_by": row["confirmation_decided_by"],
            }
        return item

    def dispute(self, actor_id: str, dispute_id: int) -> dict[str, Any]:
        self._require_any(actor_id, ("quote.write", "quote.review", "report.read"))
        row = self.connection.execute(
            "SELECT * FROM price_disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if row is None:
            raise NotFound("争议不存在")
        detail = self._dispute_list_item(row)
        confirmation = self.connection.execute(
            "SELECT * FROM price_confirmations WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if confirmation is not None:
            detail["confirmation"] = {
                "confirmation_id": int(confirmation["confirmation_id"]),
                "round_no": int(confirmation["round_no"]),
                "close_usd": confirmation["close_usd"],
                "basis": confirmation["basis"],
                "source_quote_id": confirmation["source_quote_id"],
                "decided_by": confirmation["decided_by"],
                "decided_at": confirmation["decided_at"],
                "consumed": bool(confirmation["consumed"]),
            }
        return detail

    def decide_dispute(self, actor_id: str, dispute_id: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.review")
        decision = required_text(raw.get("decision"), "decision", 16)
        if decision not in DISPUTE_DECISIONS:
            raise ValidationFailed("decision 必须是 select、adjudicate 或 return")
        reason = required_text(raw.get("reason"), "reason", 512)
        with transaction(self.connection, immediate=True):
            dispute = self.connection.execute(
                "SELECT * FROM price_disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if dispute is None:
                raise NotFound("争议不存在")
            if dispute["state"] != "open":
                raise InvalidState("争议已经形成结论，结论不可覆盖")
            candidates = self.connection.execute(
                "SELECT q.quote_id,q.close_usd,q.recorded_by FROM price_dispute_candidates pc "
                "JOIN price_index_quotes q ON q.quote_id=pc.quote_id WHERE pc.dispute_id=? ORDER BY q.quote_id",
                (dispute_id,),
            ).fetchall()
            submitters = {row["recorded_by"] for row in candidates}
            if actor_id in submitters:
                raise Forbidden("提交报价的人不能复核自己的记录")

            if decision == "return":
                close_usd: Decimal | None = None
                basis = None
                source_quote_id = None
            elif decision == "select":
                if not isinstance(raw.get("quote_id"), int) or isinstance(raw.get("quote_id"), bool):
                    raise ValidationFailed("select 决定必须提供候选 quote_id")
                chosen = next((row for row in candidates if row["quote_id"] == raw["quote_id"]), None)
                if chosen is None:
                    raise ValidationFailed("quote_id 不属于该争议候选")
                close_usd = Decimal(chosen["close_usd"])
                basis = "candidate_selected"
                source_quote_id = int(chosen["quote_id"])
            else:
                close_usd = decimal_value(raw.get("close_usd"), "close_usd", minimum=Decimal("0.01"))
                basis = "adjudicated"
                source_quote_id = None

            cursor = self.connection.execute(
                "UPDATE price_disputes SET state=?,decision_reason=?,decided_at=? "
                "WHERE dispute_id=? AND state='open'",
                (
                    "returned" if decision == "return" else "decided",
                    reason,
                    self._now(),
                    dispute_id,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState("争议已经形成结论，结论不可覆盖")

            if decision == "return":
                self._audit("price_dispute", str(dispute_id), "dispute.returned", actor_id, {"reason": reason})
                return {"dispute_id": dispute_id, "state": "returned", "round_no": int(dispute["round_no"])}

            try:
                confirmation_cursor = self.connection.execute(
                    "INSERT INTO price_confirmations(price_index,trade_date,round_no,dispute_id,close_usd,basis,"
                    "source_quote_id,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        dispute["price_index"],
                        dispute["trade_date"],
                        dispute["round_no"],
                        dispute_id,
                        decimal_text(close_usd),
                        basis,
                        source_quote_id,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该轮争议已经存在确认值") from exc
            confirmation_id = int(confirmation_cursor.lastrowid)
            self._audit(
                "price_dispute",
                str(dispute_id),
                "dispute.decided",
                actor_id,
                {
                    "confirmation_id": confirmation_id,
                    "basis": basis,
                    "close_usd": decimal_text(close_usd),
                    "reason": reason,
                },
            )
        return {
            "dispute_id": dispute_id,
            "state": "decided",
            "round_no": int(dispute["round_no"]),
            "confirmation_id": confirmation_id,
            "close_usd": decimal_text(close_usd),
            "basis": basis,
        }

    def _confirmed_price_row(self, price_index: str, trade_date: str | None = None) -> sqlite3.Row | None:
        if trade_date is None:
            return self.connection.execute(
                "SELECT * FROM price_confirmations WHERE price_index=? "
                "ORDER BY trade_date DESC,round_no DESC,confirmation_id DESC LIMIT 1",
                (price_index,),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM price_confirmations WHERE price_index=? AND trade_date=? "
            "ORDER BY round_no DESC,confirmation_id DESC LIMIT 1",
            (price_index, trade_date),
        ).fetchone()

    def price_summary(self, price_index: str, sessions: int = 20) -> dict[str, Any]:
        index = price_index.upper()
        rows = self.connection.execute(
            "SELECT c.trade_date,c.close_usd FROM price_confirmations c "
            "JOIN (SELECT trade_date,max(round_no) round_no FROM price_confirmations "
            "WHERE price_index=? GROUP BY trade_date) latest "
            "ON latest.trade_date=c.trade_date AND latest.round_no=c.round_no "
            "WHERE c.price_index=? ORDER BY c.trade_date DESC LIMIT ?",
            (index, index, sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_usd"])) for row in rows]
        if not points:
            raise NotFound("没有已确认基准报价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "price_index": index,
            "latest": {"trade_date": latest.trade_date, "close_usd": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_valuation_snapshot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "valuation.write")
        snapshot_id = required_text(raw.get("snapshot_id"), "snapshot_id", 64)
        price_index = required_text(raw.get("price_index"), "price_index", 16).upper()
        if price_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("price_index 必须是 BRENT、WTI、DUBAI、ESPO 或 URAL")
        trade_date = date_text(raw.get("trade_date"), "trade_date")
        positions = raw.get("positions")
        if not isinstance(positions, list) or not positions:
            raise ValidationFailed("positions 必须是非空数组")
        with transaction(self.connection, immediate=True):
            confirmation = self._confirmed_price_row(price_index, trade_date)
            if confirmation is None:
                raise InvalidState("该交易日没有已确认价格，不能生成估值快照")
            try:
                valuation = mark_to_market(
                    positions, {price_index: Decimal(confirmation["close_usd"])}
                )
            except (ValueError, TypeError) as exc:
                raise ValidationFailed(str(exc)) from exc
            try:
                self.connection.execute(
                    "INSERT INTO price_valuation_snapshots(snapshot_id,price_index,trade_date,confirmation_id,"
                    "close_usd,positions_json,valuation_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        price_index,
                        trade_date,
                        confirmation["confirmation_id"],
                        confirmation["close_usd"],
                        canonical_json(positions),
                        canonical_json(valuation),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("快照编号冲突或复核结论不可用") from exc
            self.connection.execute(
                "UPDATE price_confirmations SET consumed=1 WHERE confirmation_id=?",
                (confirmation["confirmation_id"],),
            )
            self._audit(
                "valuation_snapshot",
                snapshot_id,
                "valuation.snapshot_created",
                actor_id,
                {
                    "price_index": price_index,
                    "trade_date": trade_date,
                    "confirmation_id": int(confirmation["confirmation_id"]),
                },
            )
        return {
            "snapshot_id": snapshot_id,
            "price_index": price_index,
            "trade_date": trade_date,
            "confirmation_id": int(confirmation["confirmation_id"]),
            "close_usd": confirmation["close_usd"],
            "valuation": valuation,
        }

    def valuation_snapshot(self, actor_id: str, snapshot_id: str) -> dict[str, Any]:
        self._require_any(actor_id, ("report.read", "quote.review"))
        row = self.connection.execute(
            "SELECT * FROM price_valuation_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("估值快照不存在")
        return {
            "snapshot_id": row["snapshot_id"],
            "price_index": row["price_index"],
            "trade_date": row["trade_date"],
            "confirmation_id": int(row["confirmation_id"]),
            "close_usd": row["close_usd"],
            "positions": json.loads(row["positions_json"]),
            "valuation": json.loads(row["valuation_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

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
        price_row = self.connection.execute(
            "SELECT close_usd,confirmation_id,price_index FROM price_confirmations "
            "WHERE trade_date<=? ORDER BY trade_date DESC,round_no DESC,confirmation_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
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
            "confirmation_id": price_row["confirmation_id"],
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
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE price_confirmations SET consumed=1 WHERE confirmation_id=? AND consumed=0",
                (price_row["confirmation_id"],),
            )
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

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
