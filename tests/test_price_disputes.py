from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState
from oil_supply.service import SupplyService
from oil_supply.storage import connect


def quote_payload(day: str, close: str, source: str, *, index: str = "BRENT") -> dict[str, object]:
    return {
        "price_index": index,
        "trade_date": day,
        "close_usd": close,
        "source_revision": source,
        "observed_at": f"{day}T21:00:00Z",
    }


class PriceDisputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan-a", "planner"),
            ("plan-b", "planner"),
            ("risk", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.day = "2026-09-23"

    def tearDown(self) -> None:
        self.connection.close()

    def test_first_candidate_is_confirmed_and_duplicate_source_rejected(self) -> None:
        result = self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        self.assertEqual(result["state"], "confirmed")
        with self.assertRaises(Conflict):
            self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        summary = self.service.price_summary("BRENT")
        self.assertEqual(summary["latest"]["close_usd"], "98.00")

    def test_candidates_within_tolerance_keep_confirmed_value(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        outcome = self.service.record_quote("plan-b", quote_payload(self.day, "98.30", "src-exchange"))
        self.assertEqual(outcome["state"], "within_tolerance")
        self.assertEqual(self.service.dispute_queue("risk")["disputes"], [])
        summary = self.service.price_summary("BRENT")
        self.assertEqual(summary["latest"]["close_usd"], "98.00")

    def test_gap_beyond_tolerance_opens_dispute_and_hides_day_from_summary(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        outcome = self.service.record_quote("plan-b", quote_payload(self.day, "101.00", "src-exchange"))
        self.assertEqual(outcome["state"], "disputed")
        self.assertEqual(outcome["round"], 2)
        queue = self.service.dispute_queue("risk")
        self.assertEqual(len(queue["disputes"]), 1)
        dispute = queue["disputes"][0]
        self.assertEqual(dispute["max_gap_usd"], "3.00")
        self.assertEqual({c["source_revision"] for c in dispute["candidates"]}, {"src-ice", "src-exchange"})
        with self.assertRaises(Exception):
            self.service.price_summary("BRENT")

    def test_configurable_tolerance_is_honored(self) -> None:
        self.service.configure_price_tolerance("risk", "BRENT", "5")
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        outcome = self.service.record_quote("plan-b", quote_payload(self.day, "101.00", "src-exchange"))
        self.assertEqual(outcome["state"], "within_tolerance")
        with self.assertRaises(Forbidden):
            self.service.configure_price_tolerance("plan-a", "BRENT", "1")

    def test_late_candidate_joins_open_dispute_without_new_round(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        second = self.service.record_quote("plan-b", quote_payload(self.day, "101.00", "src-exchange"))
        third = self.service.record_quote("plan-a", quote_payload(self.day, "95.00", "src-ice-late"))
        self.assertEqual(third["state"], "disputed")
        self.assertEqual(third["dispute_id"], second["dispute_id"])
        dispute = self.service.dispute("risk", second["dispute_id"])
        self.assertEqual(len(dispute["candidates"]), 3)
        self.assertEqual(dispute["max_gap_usd"], "6.00")

    def test_reviewer_can_select_candidate_and_submitter_cannot_self_review(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        opened = self.service.record_quote("plan-b", quote_payload(self.day, "101.00", "src-exchange"))
        with self.assertRaises(Forbidden):
            self.service.resolve_dispute("plan-a", opened["dispute_id"], "select_candidate", "依据", selected_quote_id=1)
        with self.assertRaises(Forbidden):
            self.service.resolve_dispute("plan-b", opened["dispute_id"], "adjudicate", "依据", close_usd="99")
        chosen = next(c for c in self.service.dispute("risk", opened["dispute_id"])["candidates"] if c["source_revision"] == "src-exchange")
        result = self.service.resolve_dispute(
            "risk", opened["dispute_id"], "select_candidate", "交易所报价附有清算记录",
            selected_quote_id=chosen["quote_id"],
        )
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["close_usd"], "101.00")
        self.assertEqual(self.service.price_summary("BRENT")["latest"]["close_usd"], "101.00")
        with self.assertRaises(InvalidState):
            self.service.resolve_dispute("risk", opened["dispute_id"], "adjudicate", "覆盖", close_usd="97")

    def test_adjudicated_value_is_required_for_unknown_candidate(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        opened = self.service.record_quote("plan-b", quote_payload(self.day, "101.00", "src-exchange"))
        with self.assertRaises(Exception):
            self.service.resolve_dispute("risk", opened["dispute_id"], "select_candidate", "x", selected_quote_id=9999)
        result = self.service.resolve_dispute("risk", opened["dispute_id"], "adjudicate", "取两家均值", close_usd="99.50")
        self.assertEqual(result["close_usd"], "99.50")
        history = self.service.price_history("audit", "BRENT", self.day)
        self.assertEqual([d["action"] for d in history["decisions"]], ["adjudicate"])
        self.assertEqual(history["confirmed_values"][-1]["basis"], "adjudicated")

    def test_return_for_evidence_requires_new_round(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        opened = self.service.record_quote("plan-b", quote_payload(self.day, "101.00", "src-exchange"))
        returned = self.service.resolve_dispute("risk", opened["dispute_id"], "return", "双方凭证不足，退回补证")
        self.assertEqual(returned["state"], "returned")
        self.assertEqual(self.service.dispute_queue("risk")["disputes"], [])
        new_evidence = self.service.record_quote("plan-a", quote_payload(self.day, "98.20", "src-ice-v2"))
        self.assertEqual(new_evidence["state"], "disputed")
        self.assertGreater(new_evidence["round"], opened["round"])

    def test_consumed_conclusion_is_locked_and_late_quote_opens_new_round(self) -> None:
        first = self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        positions = [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "100", "entry_price_usd": "95"}]
        snapshot = self.service.valuation_snapshot("risk", self.day, positions)
        self.assertEqual(snapshot["price_refs"][0]["confirmed_id"], 1)
        locked = self.connection.execute(
            "SELECT consumed_at FROM price_confirmed_values WHERE confirmed_id=1"
        ).fetchone()
        self.assertIsNotNone(locked["consumed_at"])
        late = self.service.record_quote("plan-b", quote_payload(self.day, "105.00", "src-exchange"))
        self.assertEqual(late["state"], "disputed")
        self.assertGreater(late["round"], first["round"])
        history = self.service.price_history("audit", "BRENT", self.day)
        first_confirmed = next(v for v in history["confirmed_values"] if v["round"] == 1)
        self.assertEqual(first_confirmed["close_usd"], "98.00")
        self.assertIsNotNone(first_confirmed["consumed_at"])
        # 争议期间旧的已确认值仍在摘要中生效
        self.assertEqual(self.service.price_summary("BRENT")["latest"]["close_usd"], "98.00")
        chosen = next(c for c in self.service.dispute("risk", late["dispute_id"])["candidates"] if c["source_revision"] == "src-exchange")
        self.service.resolve_dispute("risk", late["dispute_id"], "select_candidate", "补件后采信", selected_quote_id=chosen["quote_id"])
        self.assertEqual(self.service.price_summary("BRENT")["latest"]["close_usd"], "105.00")
        rows = self.connection.execute(
            "SELECT round,close_usd,state,consumed_at FROM price_confirmed_values ORDER BY round"
        ).fetchall()
        self.assertEqual([(r["round"], r["state"]) for r in rows], [(1, "superseded"), (2, "active")])
        self.assertEqual(rows[0]["close_usd"], "98.00")
        self.assertIsNotNone(rows[0]["consumed_at"])

    def test_only_confirmed_values_feed_scenario_runs(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        self.service.create_facility("plan-a", {"facility_id": "fac-a", "name": "油田", "kind": "storage", "timezone": "UTC", "capacity_barrels": "10"})
        self.service.create_facility("plan-a", {"facility_id": "fac-b", "name": "终端", "kind": "terminal", "timezone": "UTC", "capacity_barrels": "10"})
        self.service.create_route("plan-a", {"route_id": "route-a", "origin_id": "fac-a", "destination_id": "fac-b", "product": "crude", "daily_capacity": "10", "loss_basis_points": 0, "transit_hours": 1})
        self.service.create_scenario("plan-a", {"scenario_id": "scn-1", "name": "情景", "price_index_drop_percent": "0", "route_capacity_changes": {}, "demand_changes": {}})
        self.service.approve_scenario("risk", "scn-1", 1)
        run = self.service.run_scenario("plan-a", "scn-1", self.day)
        self.assertEqual(run["price_confirmed_id"], 1)
        self.service.record_quote("plan-b", quote_payload(self.day, "120.00", "src-exchange"))
        replay = self.service.run_scenario("plan-a", "scn-1", self.day)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM scenario_runs").fetchone()[0], 1)

    def test_snapshot_requires_confirmed_value(self) -> None:
        self.service.record_quote("plan-a", quote_payload(self.day, "98.00", "src-ice"))
        self.service.record_quote("plan-b", quote_payload(self.day, "120.00", "src-exchange"))
        with self.assertRaises(InvalidState):
            self.service.valuation_snapshot(
                "risk", self.day,
                [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "1", "entry_price_usd": "90"}],
            )


class ConcurrentDisputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "concurrent.sqlite3"
        connection = connect(self.path)
        service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        for user_id, role in (("plan-a", "planner"), ("plan-b", "planner"), ("risk-a", "risk"), ("risk-b", "risk")):
            service.create_user(user_id, user_id, role)
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _service(self) -> SupplyService:
        return SupplyService(connect(self.path), FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))

    def test_concurrent_reviews_produce_single_decision(self) -> None:
        service = self._service()
        service.record_quote("plan-a", quote_payload("2026-09-23", "98.00", "src-ice"))
        opened = service.record_quote("plan-b", quote_payload("2026-09-23", "101.00", "src-exchange"))
        dispute_id = opened["dispute_id"]
        service.connection.close()
        errors: list[Exception] = []

        def review(actor: str, quote_id: int) -> None:
            own = self._service()
            try:
                own.resolve_dispute(actor, dispute_id, "select_candidate", f"{actor} 依据", selected_quote_id=quote_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                own.connection.close()

        conn = connect(self.path)
        ids = {row["source_revision"]: row["quote_id"] for row in conn.execute("SELECT quote_id,source_revision FROM price_index_quotes")}
        conn.close()
        threads = [
            threading.Thread(target=review, args=("risk-a", ids["src-ice"])),
            threading.Thread(target=review, args=("risk-b", ids["src-exchange"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        verify = connect(self.path)
        decisions = verify.execute("SELECT count(*) FROM price_dispute_decisions WHERE dispute_id=?", (dispute_id,)).fetchone()[0]
        active = verify.execute(
            "SELECT count(*) FROM price_confirmed_values WHERE price_index='BRENT' AND trade_date='2026-09-23' AND state='active'"
        ).fetchone()[0]
        state = verify.execute("SELECT state FROM price_disputes WHERE dispute_id=?", (dispute_id,)).fetchone()[0]
        verify.close()
        self.assertEqual(decisions, 1)
        self.assertEqual(active, 1)
        self.assertEqual(state, "resolved")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], (Conflict, InvalidState))

    def test_concurrent_candidates_open_exactly_one_dispute(self) -> None:
        def record(actor: str, source: str, close: str) -> None:
            own = self._service()
            try:
                own.record_quote(actor, quote_payload("2026-09-23", close, source))
            finally:
                own.connection.close()

        threads = [
            threading.Thread(target=record, args=("plan-a", "src-ice", "98.00")),
            threading.Thread(target=record, args=("plan-b", "src-exchange", "102.00")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        verify = connect(self.path)
        disputes = verify.execute(
            "SELECT count(*) FROM price_disputes WHERE price_index='BRENT' AND trade_date='2026-09-23' AND state='open'"
        ).fetchone()[0]
        candidates = verify.execute(
            "SELECT count(*) FROM price_index_quotes WHERE price_index='BRENT' AND trade_date='2026-09-23'"
        ).fetchone()[0]
        verify.close()
        self.assertEqual(disputes, 1)
        self.assertEqual(candidates, 2)


if __name__ == "__main__":
    unittest.main()
