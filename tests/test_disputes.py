from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Forbidden, InvalidState, NotFound
from oil_supply.service import SupplyService
from oil_supply.storage import connect

BRENT_DAY = "2026-09-23"


def quote_payload(close: str, revision: str, *, minute: int = 0) -> dict[str, object]:
    return {
        "price_index": "BRENT",
        "trade_date": BRENT_DAY,
        "close_usd": close,
        "source_revision": revision,
        "observed_at": f"2026-09-23T21:{minute:02d}:00Z",
    }


class DisputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("plan2", "planner"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def test_sources_kept_separately_and_within_tolerance_auto_confirms(self) -> None:
        first = self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        self.assertEqual(first["effect"], "recorded")
        with self.assertRaises(NotFound):
            self.service.price_summary("BRENT")
        second = self.service.record_quote("plan2", quote_payload("98.20", "src-b", minute=3))
        self.assertEqual(second["effect"], "auto_confirmed")
        summary = self.service.price_summary("BRENT")
        self.assertEqual(summary["latest"]["close_usd"], "98.20")
        rows = self.connection.execute(
            "SELECT source_revision,close_usd FROM price_index_quotes ORDER BY quote_id"
        ).fetchall()
        self.assertEqual([dict(row) for row in rows], [
            {"source_revision": "src-a", "close_usd": "98.00"},
            {"source_revision": "src-b", "close_usd": "98.20"},
        ])

    def test_spread_beyond_tolerance_opens_dispute_and_blocks_consumers(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        result = self.service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        self.assertEqual(result["effect"], "dispute_opened")
        dispute_id = result["dispute_id"]
        queue = self.service.dispute_queue("risk")
        self.assertEqual(len(queue["disputes"]), 1)
        item = queue["disputes"][0]
        self.assertEqual(item["spread_usd"], "1.00")
        self.assertEqual(item["tolerance_usd"], "0.50")
        self.assertEqual(item["candidate_count"], 2)
        with self.assertRaises(NotFound):
            self.service.price_summary("BRENT")
        with self.assertRaises(InvalidState):
            self.service.create_valuation_snapshot("risk", {
                "snapshot_id": "snap-1",
                "price_index": "BRENT",
                "trade_date": BRENT_DAY,
                "positions": [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "100", "entry_price_usd": "98"}],
            })
        detail = self.service.dispute("risk", dispute_id)
        self.assertEqual({c["source_revision"] for c in detail["candidates"]}, {"src-a", "src-b"})

    def test_reviewer_can_select_candidate(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        result = self.service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        quote_b = self.connection.execute(
            "SELECT quote_id FROM price_index_quotes WHERE source_revision='src-b'"
        ).fetchone()["quote_id"]
        decision = self.service.decide_dispute("risk", result["dispute_id"], {
            "decision": "select", "quote_id": quote_b, "reason": "采用交易所收盘",
        })
        self.assertEqual(decision["state"], "decided")
        self.assertEqual(decision["close_usd"], "99.00")
        self.assertEqual(decision["basis"], "candidate_selected")
        self.assertEqual(self.service.price_summary("BRENT")["latest"]["close_usd"], "99.00")
        self.assertEqual(self.service.dispute_queue("risk")["disputes"], [])

    def test_reviewer_can_enter_adjudicated_value(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        opened = self.service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        decision = self.service.decide_dispute("risk", opened["dispute_id"], {
            "decision": "adjudicate", "close_usd": "98.55", "reason": "按结算委员会核定",
        })
        self.assertEqual(decision["basis"], "adjudicated")
        self.assertEqual(decision["close_usd"], "98.55")
        history = self.service.dispute_history("risk", "BRENT", BRENT_DAY)
        self.assertEqual(history["disputes"][0]["decision"]["close_usd"], "98.55")

    def test_return_for_evidence_then_new_candidate_opens_new_round(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        opened = self.service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        returned = self.service.decide_dispute("risk", opened["dispute_id"], {
            "decision": "return", "reason": "两份来源均缺少签章",
        })
        self.assertEqual(returned["state"], "returned")
        with self.assertRaises(NotFound):
            self.service.price_summary("BRENT")
        follow_up = self.service.record_quote("plan2", quote_payload("98.10", "src-c", minute=8))
        self.assertEqual(follow_up["effect"], "dispute_opened")
        self.assertTrue(follow_up["supplements_evidence"])
        self.assertEqual(follow_up["dispute_id"], opened["dispute_id"] + 1)
        detail = self.service.dispute("risk", follow_up["dispute_id"])
        self.assertEqual(detail["round_no"], 2)
        self.assertEqual(detail["candidate_count"], 3)

    def test_submitter_cannot_review_own_record(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        opened = self.service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        with self.assertRaises(Forbidden):
            self.service.decide_dispute("plan", opened["dispute_id"], {
                "decision": "adjudicate", "close_usd": "98.5", "reason": "自己复核",
            })
        with self.assertRaises(Forbidden):
            self.service.decide_dispute("plan2", opened["dispute_id"], {
                "decision": "adjudicate", "close_usd": "98.5", "reason": "自己复核",
            })
        with self.assertRaises(Forbidden):
            self.service.decide_dispute("audit", opened["dispute_id"], {
                "decision": "return", "reason": "审计员无复核权",
            })

    def test_decision_is_final_and_used_conclusion_cannot_be_overwritten(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        opened = self.service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        self.service.decide_dispute("risk", opened["dispute_id"], {
            "decision": "adjudicate", "close_usd": "98.5", "reason": "核定",
        })
        with self.assertRaises(InvalidState):
            self.service.decide_dispute("risk", opened["dispute_id"], {
                "decision": "adjudicate", "close_usd": "100", "reason": "改主意",
            })
        snapshot = self.service.create_valuation_snapshot("risk", {
            "snapshot_id": "snap-1",
            "price_index": "BRENT",
            "trade_date": BRENT_DAY,
            "positions": [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "100", "entry_price_usd": "98"}],
        })
        self.assertEqual(snapshot["valuation"]["unrealized_pnl_usd"], "50.00")
        consumed = self.connection.execute(
            "SELECT consumed FROM price_confirmations WHERE confirmation_id=?",
            (snapshot["confirmation_id"],),
        ).fetchone()["consumed"]
        self.assertEqual(consumed, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE price_confirmations SET close_usd='1.0' WHERE confirmation_id=?",
                (snapshot["confirmation_id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM price_confirmations WHERE confirmation_id=?",
                (snapshot["confirmation_id"],),
            )

    def test_consumed_confirmation_can_be_reused_but_never_mutated(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        self.service.record_quote("plan2", quote_payload("98.10", "src-b", minute=3))
        positions = [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "100", "entry_price_usd": "98"}]
        first = self.service.create_valuation_snapshot("risk", {
            "snapshot_id": "snap-1", "price_index": "BRENT", "trade_date": BRENT_DAY, "positions": positions,
        })
        second = self.service.create_valuation_snapshot("risk", {
            "snapshot_id": "snap-2", "price_index": "BRENT", "trade_date": BRENT_DAY, "positions": positions,
        })
        self.assertEqual(first["confirmation_id"], second["confirmation_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE price_confirmations SET close_usd='1.0' WHERE confirmation_id=?",
                (first["confirmation_id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE price_confirmations SET consumed=0 WHERE confirmation_id=?",
                (first["confirmation_id"],),
            )

    def test_late_conflicting_candidate_after_used_value_opens_new_round(self) -> None:
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        self.service.record_quote("plan2", quote_payload("98.10", "src-b", minute=3))
        confirmation = self.connection.execute(
            "SELECT * FROM price_confirmations WHERE trade_date=?", (BRENT_DAY,)
        ).fetchone()
        self.service.create_valuation_snapshot("risk", {
            "snapshot_id": "snap-1",
            "price_index": "BRENT",
            "trade_date": BRENT_DAY,
            "positions": [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "10", "entry_price_usd": "98"}],
        })
        late = self.service.record_quote("plan", quote_payload("110.00", "src-c", minute=20))
        self.assertEqual(late["effect"], "dispute_opened")
        detail = self.service.dispute("risk", late["dispute_id"])
        self.assertEqual(detail["round_no"], 2)
        snapshot = self.service.valuation_snapshot("risk", "snap-1")
        self.assertEqual(snapshot["close_usd"], confirmation["close_usd"])

    def test_configurable_tolerance(self) -> None:
        self.service.set_tolerance("risk", {"price_index": "BRENT", "tolerance_usd": "2.00"})
        self.service.record_quote("plan", quote_payload("98.00", "src-a"))
        result = self.service.record_quote("plan2", quote_payload("99.50", "src-b", minute=3))
        self.assertEqual(result["effect"], "auto_confirmed")
        with self.assertRaises(Forbidden):
            self.service.set_tolerance("plan", {"price_index": "BRENT", "tolerance_usd": "0.1"})


class DisputeConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "concurrency.sqlite3"
        init = SupplyService(connect(self.path), FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        for user_id, role in (("plan", "planner"), ("plan2", "planner"), ("risk", "risk"), ("risk2", "risk"), ("audit", "auditor")):
            init.create_user(user_id, user_id, role)
        init.connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _service(self) -> SupplyService:
        return SupplyService(connect(self.path), FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))

    def test_concurrent_candidates_produce_single_dispute(self) -> None:
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def record(actor: str, revision: str, close: str) -> None:
            service = self._service()
            try:
                barrier.wait()
                service.record_quote(actor, quote_payload(close, revision))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                service.connection.close()

        threads = [
            threading.Thread(target=record, args=("plan", "src-a", "98.00")),
            threading.Thread(target=record, args=("plan2", "src-b", "105.00")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        service = self._service()
        disputes = service.connection.execute(
            "SELECT count(*) AS n FROM price_disputes WHERE trade_date=?", (BRENT_DAY,)
        ).fetchone()["n"]
        self.assertEqual(disputes, 1)
        candidates = service.connection.execute(
            "SELECT count(*) AS n FROM price_dispute_candidates"
        ).fetchone()["n"]
        self.assertEqual(candidates, 2)
        service.connection.close()

    def test_concurrent_reviews_produce_single_valid_conclusion(self) -> None:
        service = self._service()
        service.record_quote("plan", quote_payload("98.00", "src-a"))
        opened = service.record_quote("plan2", quote_payload("99.00", "src-b", minute=3))
        service.connection.close()
        dispute_id = opened["dispute_id"]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def review(actor: str, close: str) -> None:
            worker = self._service()
            barrier.wait()
            try:
                worker.decide_dispute(actor, dispute_id, {
                    "decision": "adjudicate", "close_usd": close, "reason": f"{actor} 核定",
                })
                with lock:
                    outcomes.append("won")
            except InvalidState:
                with lock:
                    outcomes.append("lost")
            finally:
                worker.connection.close()

        threads = [
            threading.Thread(target=review, args=("risk", "98.50")),
            threading.Thread(target=review, args=("risk2", "99.50")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["lost", "won"])
        checker = self._service()
        confirmations = checker.connection.execute(
            "SELECT close_usd FROM price_confirmations WHERE dispute_id=?", (dispute_id,)
        ).fetchall()
        self.assertEqual(len(confirmations), 1)
        self.assertIn(confirmations[0]["close_usd"], {"98.50", "99.50"})
        state = checker.connection.execute(
            "SELECT state FROM price_disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()["state"]
        self.assertEqual(state, "decided")
        self.assertTrue(checker.audit_chain("audit")["valid"])
        checker.connection.close()


class DisputeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        )
        self.app = JsonApplication(self.service)
        self.service.create_user("plan", "plan", "planner")
        self.service.create_user("risk", "risk", "risk")

    def tearDown(self) -> None:
        self.connection.close()

    def test_queue_and_decide_endpoints(self) -> None:
        first = self.app.handle("POST", "/quotes", {"X-Actor-Id": "plan"}, json.dumps(quote_payload("98", "src-a")).encode())
        self.assertEqual(first.status, 201)
        second = self.app.handle("POST", "/quotes", {"X-Actor-Id": "plan"}, json.dumps(quote_payload("100", "src-b", minute=2)).encode())
        dispute_id = second.body["dispute_id"]
        queue = self.app.handle("GET", "/disputes", {"X-Actor-Id": "risk"})
        self.assertEqual(queue.status, 200)
        self.assertEqual(queue.body["disputes"][0]["dispute_id"], dispute_id)
        forbidden = self.app.handle(
            "POST", f"/disputes/{dispute_id}/decide", {"X-Actor-Id": "plan"},
            json.dumps({"decision": "return", "reason": "无权"}).encode(),
        )
        self.assertEqual(forbidden.status, 403)
        decided = self.app.handle(
            "POST", f"/disputes/{dispute_id}/decide", {"X-Actor-Id": "risk"},
            json.dumps({"decision": "return", "reason": "缺凭证"}).encode(),
        )
        self.assertEqual(decided.status, 200)
        self.assertEqual(decided.body["state"], "returned")
        history = self.app.handle("GET", "/disputes/history?price_index=BRENT", {"X-Actor-Id": "risk"})
        self.assertEqual(history.status, 200)
        self.assertEqual(history.body["disputes"][0]["state"], "returned")


if __name__ == "__main__":
    unittest.main()
