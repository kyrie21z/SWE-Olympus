"""Trusted Momus fixture: copied and frozen outside the implementation write scope."""

from concurrent.futures import ThreadPoolExecutor
import unittest

from packages.t03.campus import cancel, join, new_activity, query


class CampusAcceptance(unittest.TestCase):
    def test_capacity_under_concurrent_join(self):
        activity = new_activity(1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: join(activity, str(i)), range(30)))
        self.assertEqual(results.count("CONFIRMED"), 1)

    def test_duplicate_does_not_occupy_twice(self):
        activity = new_activity(2)
        self.assertEqual(join(activity, "a"), "CONFIRMED")
        self.assertEqual(join(activity, "a"), "CONFIRMED")
        self.assertEqual(join(activity, "b"), "CONFIRMED")
        self.assertEqual(len(activity["records"]), 2)

    def test_cancelled_candidate_is_skipped_and_next_is_promoted(self):
        activity = new_activity(1)
        for account in ["a", "b", "c", "d"]:
            join(activity, account)
        cancel(activity, "b")
        cancel(activity, "a")
        self.assertEqual(query(activity, "b"), "CANCELLED")
        self.assertEqual(query(activity, "c"), "CONFIRMED")
        self.assertEqual(query(activity, "d"), "WAITLISTED")
        cancel(activity, "a")
        self.assertEqual(query(activity, "d"), "WAITLISTED")

    def test_active_query(self):
        activity = new_activity(1)
        self.assertIsNone(query(activity, "missing"))
        join(activity, "a")
        self.assertEqual(query(activity, "a"), "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
