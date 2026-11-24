"""python -m unittest discover -s tests (stdlib only; CI: scripts job)."""
import importlib.util
import pathlib
import unittest

path = pathlib.Path(__file__).parent.parent / "scripts" / "gpu-chargeback.py"
spec = importlib.util.spec_from_file_location("gpu_chargeback", path)
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

NODES = {
    "od": {"hours": 1.0, "price": 0.5, "capacity": "on-demand"},
    "spot": {"hours": 1.0, "price": 0.2, "capacity": "spot"},
}


def alloc(ns, node, hours, cost=0.0):
    return {"namespace": ns, "node": node, "gpu_hours": hours, "gpu_cost": cost}


class Chargeback(unittest.TestCase):
    def test_borrowed_share_is_paid_by_the_borrower_at_that_nodes_price(self):
        _, totals, _ = cb.chargeback(
            [alloc("llm-platform", "od", 1.0), alloc("training", "od", 1.0), alloc("training", "spot", 2.0)],
            NODES, 2)
        self.assertAlmostEqual(totals["product"]["used"], 0.25)
        self.assertAlmostEqual(totals["research"]["used"], 0.25 + 0.2)

    def test_idle_shares_go_to_the_owner(self):
        _, totals, _ = cb.chargeback([alloc("training", "od", 0.5)], NODES, 2)
        self.assertAlmostEqual(totals["product"]["idle"], 1.5 / 2 * 0.5)
        self.assertAlmostEqual(totals["research"]["idle"], 0.2)

    def test_owed_adds_up_to_the_nodes_real_cost(self):
        rows, _, real = cb.chargeback(
            [alloc("llm-platform", "od", 0.7), alloc("training", "od", 0.3), alloc("training", "spot", 1.1)],
            NODES, 2)
        self.assertAlmostEqual(sum(r["owed"] for r in rows), real)
        self.assertAlmostEqual(real, 0.7)

    def test_other_namespaces_keep_their_name(self):
        _, totals, _ = cb.chargeback([alloc("default", "od", 1.0)], NODES, 2)
        self.assertIn("default", totals)


if __name__ == "__main__":
    unittest.main()
