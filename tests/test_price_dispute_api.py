from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.service import SupplyService


def quote(day: str, close: str, source: str) -> dict[str, object]:
    return {
        "price_index": "BRENT",
        "trade_date": day,
        "close_usd": close,
        "source_revision": source,
        "observed_at": f"{day}T21:00:00Z",
    }


class PriceDisputeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        for user_id, role in (("plan-a", "planner"), ("plan-b", "planner"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, actor: str, payload: dict[str, object]):
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode())

    def get(self, path: str, actor: str):
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_dispute_flow_over_http(self) -> None:
        self.post("/quotes", "plan-a", quote("2026-09-23", "98", "src-ice"))
        opened = self.post("/quotes", "plan-b", quote("2026-09-23", "102", "src-exchange"))
        self.assertEqual(opened.status, 201)
        self.assertEqual(opened.body["state"], "disputed")
        dispute_id = opened.body["dispute_id"]

        queue = self.get("/prices/disputes", "risk")
        self.assertEqual(queue.status, 200)
        self.assertEqual(len(queue.body["disputes"]), 1)

        detail = self.get(f"/prices/disputes/{dispute_id}", "risk")
        self.assertEqual(detail.status, 200)
        self.assertEqual(len(detail.body["candidates"]), 2)

        forbidden = self.post(
            f"/prices/disputes/{dispute_id}/decisions", "plan-a",
            {"action": "adjudicate", "close_usd": "99", "rationale": "自报自核"},
        )
        self.assertEqual(forbidden.status, 403)

        decision = self.post(
            f"/prices/disputes/{dispute_id}/decisions", "risk",
            {"action": "adjudicate", "close_usd": "99.00", "rationale": "凭证齐备，核定 99"},
        )
        self.assertEqual(decision.status, 201)
        self.assertEqual(decision.body["close_usd"], "99.00")

        history = self.get("/prices/history/BRENT?trade_date=2026-09-23", "audit")
        self.assertEqual(history.status, 200)
        self.assertEqual(len(history.body["candidates"]), 2)
        self.assertEqual(history.body["decisions"][0]["rationale"], "凭证齐备，核定 99")

    def test_tolerance_and_snapshot_endpoints(self) -> None:
        configured = self.post("/prices/tolerance", "risk", {"price_index": "BRENT", "tolerance_usd": "5"})
        self.assertEqual(configured.status, 200)
        self.post("/quotes", "plan-a", quote("2026-09-23", "98", "src-ice"))
        within = self.post("/quotes", "plan-b", quote("2026-09-23", "101", "src-exchange"))
        self.assertEqual(within.body["state"], "within_tolerance")
        snapshot = self.post("/valuation/snapshots", "risk", {
            "as_of_date": "2026-09-23",
            "positions": [{"position_id": "p1", "price_index": "BRENT", "quantity_barrels": "100", "entry_price_usd": "95"}],
        })
        self.assertEqual(snapshot.status, 201)
        self.assertEqual(snapshot.body["unrealized_pnl_usd"], "300.00")


if __name__ == "__main__":
    unittest.main()
