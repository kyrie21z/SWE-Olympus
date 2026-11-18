"""独立运行的库存验收。执行宿主会将工件目录加到 Python 搜索路径。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from inventory.service import InventoryService


def race(count, operation):
    barrier = Barrier(count, timeout=15)

    def invoke(index):
        barrier.wait()
        return operation(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(invoke, range(count), timeout=25))


class InventoryChecks(unittest.TestCase):
    def setUp(self):
        self.service = InventoryService()

    def stock(self, sku="A"):
        state = self.service.get_sku(sku)["data"]
        amounts = (state["available"], state["reserved"], state["sold"])
        self.assertEqual(state["initial"], sum(amounts))
        self.assertGreaterEqual(min(amounts), 0)
        return amounts

    def assert_error(self, result, code):
        self.assertEqual(result, {"ok": False, "error": {"code": code}})

    def test_creation_queries_and_bounds(self):
        s = self.service
        self.assertEqual(s.create_sku("A", 0), {"ok": True, "data": {
            "sku": "A", "initial": 0, "available": 0, "reserved": 0, "sold": 0,
        }})
        self.assertTrue(s.create_sku("a", 10000)["ok"])
        self.assertTrue(s.create_sku("A" * 32, 1)["ok"])
        self.assert_error(s.create_sku("A", 3), "SKU_EXISTS")
        self.assertEqual(self.stock(), (0, 0, 0))
        self.assertEqual(self.stock("a"), (10000, 0, 0))
        self.assert_error(s.get_sku("unknown"), "SKU_NOT_FOUND")
        self.assert_error(s.get_order("unknown"), "ORDER_NOT_FOUND")

    def test_invalid_inputs_are_not_coerced(self):
        s = self.service
        s.create_sku("A", 1)
        for key in ("", "a b", "中文", "a/b", "x" * 33, None):
            self.assert_error(s.create_sku(key, 1), "INVALID_ARGUMENT")
            self.assert_error(s.reserve(key, "A", 1), "INVALID_ARGUMENT")
        for quantity in (-1, 10001, True, 1.0, "1", None):
            self.assert_error(s.create_sku("B", quantity), "INVALID_ARGUMENT")
            self.assert_error(s.reserve("O", "A", quantity), "INVALID_ARGUMENT")
        self.assert_error(s.reserve("O", "A", 0), "INVALID_ARGUMENT")
        self.assertEqual(self.stock(), (1, 0, 0))

    def test_reserve_and_validate_error_precedence(self):
        s = self.service
        s.create_sku("A", 3)
        self.assert_error(s.reserve("O", "missing", 1), "SKU_NOT_FOUND")
        self.assert_error(s.reserve("O", "A", 4), "INSUFFICIENT_STOCK")
        self.assert_error(s.get_order("O"), "ORDER_NOT_FOUND")
        order = {"order_id": "O", "sku": "A", "quantity": 2, "state": "RESERVED"}
        self.assertEqual(s.reserve("O", "A", 2)["data"], order)
        self.assertEqual(s.reserve("O", "A", 2)["data"], order)
        self.assert_error(s.reserve("O", "missing", 2), "ORDER_CONFLICT")
        self.assert_error(s.reserve("O", "A", 1), "ORDER_CONFLICT")
        self.assert_error(s.reserve("O", "A", 0), "INVALID_ARGUMENT")
        self.assertEqual(self.stock(), (1, 2, 0))

    def test_terminal_transitions_and_idempotency(self):
        s = self.service
        s.create_sku("A", 3)
        s.reserve("O1", "A", 2)
        cancelled = s.cancel("O1")
        self.assertEqual(cancelled, {"ok": True, "data": {"order_id": "O1", "sku": "A", "quantity": 2, "state": "CANCELLED"}})
        self.assertEqual(self.stock(), (3, 0, 0))
        s.reserve("O2", "A", 2)
        confirmed = s.confirm("O2")
        self.assertEqual(confirmed, {"ok": True, "data": {"order_id": "O2", "sku": "A", "quantity": 2, "state": "CONFIRMED"}})
        for _ in range(10):
            self.assertEqual(s.cancel("O1"), cancelled)
            self.assertEqual(s.confirm("O2"), confirmed)
            self.assertEqual(self.stock(), (1, 0, 2))
        self.assert_error(s.confirm("O1"), "STATE_CONFLICT")
        self.assert_error(s.cancel("O2"), "STATE_CONFLICT")
        self.assert_error(s.confirm("O3"), "ORDER_NOT_FOUND")
        self.assert_error(s.cancel("O3"), "ORDER_NOT_FOUND")
        self.assertEqual(s.reserve("O1", "A", 2)["data"]["state"], "CANCELLED")
        self.assertEqual(s.reserve("O2", "A", 2)["data"]["state"], "CONFIRMED")
        self.assertEqual(self.stock(), (1, 0, 2))

    def test_returned_snapshots_and_instances_are_isolated(self):
        s = self.service
        s.create_sku("A", 2)["data"]["available"] = -1
        s.create_sku("B", 1)
        s.reserve("O", "A", 1)["data"]["state"] = "altered"
        s.get_order("O")["data"]["quantity"] = 100
        s.get_sku("A")["data"]["reserved"] = -1
        self.assertEqual(s.get_order("O")["data"]["quantity"], 1)
        self.assertEqual(self.stock(), (1, 1, 0))
        self.assertEqual(self.stock("B"), (1, 0, 0))
        self.assert_error(InventoryService().get_sku("A"), "SKU_NOT_FOUND")

    def test_stock_is_not_publicly_mutable_and_failed_order_can_be_reused(self):
        s = self.service
        with self.assertRaises(AttributeError):
            s.skus
        with self.assertRaises(AttributeError):
            s.orders
        with self.assertRaises(AttributeError):
            s.settle
        s.create_sku("A", 1)
        self.assert_error(s.reserve("O", "A", 2), "INSUFFICIENT_STOCK")
        self.assert_error(s.get_order("O"), "ORDER_NOT_FOUND")
        self.assertEqual(self.stock(), (1, 0, 0))
        result = s.reserve("O", "A", 1)
        self.assertEqual(result["data"]["state"], "RESERVED")
        self.assertEqual(self.stock(), (0, 1, 0))
        confirmed = s.confirm("O")
        confirmed["data"]["state"] = "changed"
        self.assertEqual(s.get_order("O")["data"]["state"], "CONFIRMED")

    def test_queries_during_concurrent_writes_see_consistent_snapshots(self):
        s = self.service
        s.create_sku("A", 20)

        def operation(index):
            if index < 20:
                order_id = f"O{index}"
                self.assertTrue(s.reserve(order_id, "A", 1)["ok"])
                result = s.confirm(order_id) if index < 10 else s.cancel(order_id)
                self.assertTrue(result["ok"])
            else:
                for order_index in range(20):
                    self.stock()
                    query = s.get_order(f"O{order_index}")
                    if query["ok"]:
                        self.assertIn(query["data"]["state"], {"RESERVED", "CONFIRMED", "CANCELLED"})
                    else:
                        self.assert_error(query, "ORDER_NOT_FOUND")

        race(30, operation)
        self.assertEqual(self.stock(), (10, 0, 10))

    def test_competing_orders_do_not_oversell(self):
        for _ in range(5):
            self.service = InventoryService()
            self.service.create_sku("A", 10)
            results = race(100, lambda i: self.service.reserve(f"O{i}", "A", 1))
            self.assertEqual(sum(outcome["ok"] for outcome in results), 10)
            for outcome in results:
                if not outcome["ok"]:
                    self.assert_error(outcome, "INSUFFICIENT_STOCK")
            self.assertEqual(self.stock(), (0, 10, 0))

    def test_same_order_and_opposite_terminal_actions(self):
        s = self.service
        s.create_sku("A", 10)
        results = race(100, lambda _: s.reserve("O", "A", 1))
        self.assertTrue(all(result["ok"] and result == results[0] for result in results))
        self.assertEqual(self.stock(), (9, 1, 0))
        results = race(2, lambda index: s.confirm("O") if index == 0 else s.cancel("O"))
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assert_error(next(result for result in results if not result["ok"]), "STATE_CONFLICT")
        state = s.get_order("O")["data"]["state"]
        self.assertEqual(self.stock(), (9, 0, 1) if state == "CONFIRMED" else (10, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
