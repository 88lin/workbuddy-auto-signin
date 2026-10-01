import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import signin


def response(data):
    return 200, {"code": 0, "data": data}


class MakeupTests(unittest.TestCase):
    def setUp(self):
        signin._start_budget()
        self.streak = {
            "launch_date": "2026-06-17",
            "streak": {"days": 1, "makeup_dates": ["2026-09-01"]},
            "makeup_cards": {"balance": 2},
        }
        self.heatmap = {
            "today": {"date": "2026-09-30"},
            "cells": [
                {"date": "2026-09-01", "score": 10},
                {"date": "2026-09-28", "score": 0},
                {"date": "2026-09-29", "score": 12},
                {"date": "2026-09-30", "score": 0},
            ],
        }
        self.responses = {
            "/buddy/travel/status": response({"state": "idle", "daily_limit_reached": True}),
            "/tasks": response({"tasks": []}),
            "/streak": response(self.streak),
            "/heatmap": response(self.heatmap),
            "/redeem/summary": response({"starter_status": "locked", "advanced_status": "locked",
                                         "legendary_status": "locked"}),
            "/lottery/chances": response({"balance": 0}),
            "/buddy/quota": response({"affordable": 0}),
            "/energy": response({"balance": 0}),
        }
        self.gets = []
        self.writes = []
        self.makeup_reply = response({"makeup_cards": {"balance": 1}})
        self.get_patch = patch.object(signin, "get", side_effect=self.get)
        self.post_patch = patch.object(signin, "post", side_effect=self.post)
        self.get_patch.start()
        self.post_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.addCleanup(self.post_patch.stop)

    def get(self, url, headers):
        path = url.split("/v2/activity/growth", 1)[1]
        self.gets.append(path)
        return copy.deepcopy(self.responses[path])

    def post(self, url, headers, payload=None, **kwargs):
        path = url.split("/v2/activity/growth", 1)[1]
        self.writes.append((path, payload))
        self.assertEqual(path, "/makeup-cards/use")
        return copy.deepcopy(self.makeup_reply)

    def run_growth(self):
        return signin.run_growth({}, "https://test.invalid")

    def test_repeated_runs_never_use_already_made_up_dates(self):
        # Regression: the old code submitted September 1 on every poll.
        self.heatmap["cells"][1]["score"] = 5
        for _ in range(2):
            code, out = self.run_growth()
            self.assertEqual(code, 0)
            self.assertTrue(out["idle"])
        self.assertEqual(self.writes, [])

    def test_real_gap_is_selected_instead_of_makeup_history(self):
        code, out = self.run_growth()
        self.assertEqual(code, 0)
        self.assertEqual(self.writes[0][1]["target_date"], "2026-09-28")
        self.assertEqual(len(self.writes), 1)
        self.assertIn("补登 2026-09-28", out["report"])
        self.assertEqual(self.gets.count("/streak"), 2)

    def test_empty_makeup_history_does_not_disable_first_makeup(self):
        self.streak["streak"]["makeup_dates"] = []
        self.run_growth()
        self.assertEqual([x[1]["target_date"] for x in self.writes], ["2026-09-28"])

    def test_only_latest_eligible_day_is_used_and_dates_are_deduplicated(self):
        self.heatmap["cells"] += [
            {"date": "2026-08-31", "score": 0},
            {"date": "2026-10-01", "score": 0},
            {"date": "2026-09-27", "score": 0},
            {"date": "2026-09-28", "score": 0},
        ]
        self.run_growth()
        self.assertEqual([x[1]["target_date"] for x in self.writes], ["2026-09-28"])

    def test_makeup_history_is_excluded_even_if_heatmap_is_stale(self):
        self.streak["streak"]["makeup_dates"] += ["2026-09-28"]
        code, out = self.run_growth()
        self.assertEqual((code, self.writes), (0, []))
        self.assertTrue(out["idle"])

    def test_launch_date_and_server_month_boundaries(self):
        self.streak["launch_date"] = "2026-09-29"
        self.run_growth()
        self.assertEqual(self.writes, [])
        self.streak["launch_date"] = "2026-06-17"
        self.heatmap["today"]["date"] = "2026-10-01"
        self.run_growth()
        self.assertEqual(self.writes, [])

    def test_no_cards_skips_heatmap_query(self):
        self.streak["makeup_cards"] = 0
        self.run_growth()
        self.assertNotIn("/heatmap", self.gets)
        self.assertEqual(self.writes, [])

    def test_numeric_card_balance_remains_supported(self):
        self.streak["makeup_cards"] = 2
        self.run_growth()
        self.assertEqual([x[1]["target_date"] for x in self.writes], ["2026-09-28"])

    def test_unavailable_heatmap_never_spends_cards_and_reports_failure(self):
        for status in (400, 404, 429, 500, signin.CODE_NO_NETWORK):
            with self.subTest(status=status):
                self.responses["/heatmap"] = status, {"msg": "unavailable"}
                code, out = self.run_growth()
                self.assertEqual(code, 1)
                self.assertFalse(out["idle"])
                self.assertEqual(self.writes, [])

    def test_business_error_in_calendar_query_is_not_an_empty_calendar(self):
        for path in ("/streak", "/heatmap"):
            with self.subTest(path=path):
                original = self.responses[path]
                self.responses[path] = 200, {"code": 10001, "msg": "unavailable"}
                code, out = self.run_growth()
                self.assertEqual(code, 1)
                self.assertIn("unavailable", out["report"])
                self.assertEqual(self.writes, [])
                self.responses[path] = original

    def test_unknown_makeup_rejection_needs_attention(self):
        for status in (200, 400, 409, 429, 500):
            with self.subTest(status=status):
                self.gets.clear()
                self.writes.clear()
                self.makeup_reply = status, {"code": 10001, "msg": "invalid request"}
                code, out = self.run_growth()
                self.assertEqual(code, 1)
                self.assertIn("失败", out["report"])
                self.assertEqual(self.gets.count("/streak"), 1)
                self.assertEqual(len(self.writes), 1)

    def test_date_activated_between_query_and_write_is_a_benign_race(self):
        self.makeup_reply = 400, {"msg": "date is not broken, no makeup needed"}
        code, out = self.run_growth()
        self.assertEqual(code, 0)
        self.assertNotIn("failures", out)
        self.assertTrue(out["idle"])
        self.assertNotIn("失败", out["report"])

    def test_auth_failure_on_heatmap_stops_subsequent_work(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.gets.clear()
                self.responses["/heatmap"] = status, {}
                code, out = self.run_growth()
                self.assertEqual(code, 1)
                self.assertEqual(out["http"], status)
                self.assertEqual(self.gets[-1], "/heatmap")
                self.assertEqual(self.writes, [])

    def test_invalid_calendar_cannot_be_interpreted_as_missing_activity(self):
        invalid = [None, {}, {"today": {"date": "2026-02-30"}, "cells": []},
                   {"today": {"date": "2026-09-30"}, "cells": None}]
        for cell in ({"date": "2026-09-28"}, {"date": "2026-09-28", "score": "0"},
                     {"date": "2026-09-28", "score": False},
                     {"date": "2026-09-28", "score": float("nan")},
                     {"date": "2026-09-28", "score": -1},
                     {"date": "2026-09-31", "score": 0}):
            invalid.append({"today": {"date": "2026-09-30"}, "cells": [cell]})
        for data in invalid:
            with self.subTest(data=data):
                self.responses["/heatmap"] = response(data)
                code, out = self.run_growth()
                self.assertEqual(code, 1)
                self.assertFalse(out["idle"])
                self.assertEqual(self.writes, [])

    def test_conflicting_calendar_entries_do_not_spend_a_card(self):
        self.heatmap["cells"].append({"date": "2026-09-28", "score": 10})
        code, _ = self.run_growth()
        self.assertEqual((code, self.writes), (1, []))

    def test_missing_days_are_not_guessed_and_invalid_history_stops_makeup(self):
        self.heatmap["cells"] = []
        code, _ = self.run_growth()
        self.assertEqual((code, self.writes), (0, []))
        for history in (None, "2026-09-01", ["2026-09-31"], [{}]):
            with self.subTest(history=history):
                self.streak["streak"]["makeup_dates"] = history
                code, _ = self.run_growth()
                self.assertEqual((code, self.writes), (1, []))

    def test_unknown_launch_date_stops_makeup(self):
        del self.streak["launch_date"]
        code, _ = self.run_growth()
        self.assertEqual((code, self.writes), (1, []))

    def test_travel_reward_does_not_hide_makeup_failure(self):
        self.responses["/buddy/travel/status"] = response({
            "state": "arrived", "record_id": 123, "daily_limit_reached": True})
        self.makeup_reply = 400, {"msg": "invalid request"}

        def post(url, headers, payload=None, **kwargs):
            if url.endswith("/buddy/travel/claim"):
                return response({"reward_credit": 10})
            return self.post(url, headers, payload, **kwargs)

        with patch.object(signin, "post", side_effect=post):
            code, out = self.run_growth()
        self.assertEqual(code, 1)
        self.assertEqual(out["credits_gained"], 10)
        self.assertIn("领旅行礼物 +10", out["report"])
        self.assertIn("补登 2026-09-28 失败", out["report"])

    def test_successful_signin_does_not_hide_makeup_failure(self):
        self.makeup_reply = 400, {"msg": "invalid request"}
        with patch.object(signin, "run_auto", return_value=(0, {
                "result": "CLAIMED", "credit": 100, "report": "成功领取 100 积分"})):
            code, out, quiet = signin.run_daily({}, "https://test.invalid")
        self.assertEqual(code, 1)
        self.assertEqual((out["result"], out["credit"]), ("CLAIMED", 100))
        self.assertTrue(out["needs_attention"])
        self.assertFalse(quiet)
        self.assertIn("补登 2026-09-28 失败", out["report"])

    def test_silent_poll_and_legacy_alias_log_makeup_failure(self):
        self.makeup_reply = 400, {"msg": "invalid request"}
        session = {"auth": {"accessToken": "synthetic-token", "endpoint": "https://test.invalid"},
                   "account": {"uid": "synthetic-user"}}
        with tempfile.TemporaryDirectory() as temp:
            auth = Path(temp) / "auth.json"
            log = Path(temp) / "signin.log"
            auth.write_text(json.dumps(session), encoding="utf-8")
            with patch.dict(os.environ, {"WORKBUDDY_AUTH_FILE": str(auth),
                                         "WORKBUDDY_SIGNIN_LOG": str(log)}), \
                    patch.object(signin, "run_auto", return_value=(0, {
                        "result": "ALREADY", "report": "今日已签过"})):
                for action in ("silent-poll", "silent-growth"):
                    self.assertEqual(signin._run(action), 1)
            entries = [json.loads(line.split("] ", 1)[1]) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertTrue(entry["needs_attention"])
            self.assertEqual(entry["result"], "ALREADY")
            self.assertEqual(entry["trigger"], "poll")
            self.assertIn("补登 2026-09-28 失败", entry["growth"])
            self.assertIn("补登 2026-09-28 失败", entry["report"])
            self.assertNotIn("synthetic-token", json.dumps(entry))


if __name__ == "__main__":
    unittest.main()
