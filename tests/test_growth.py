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


class BuddyLifecycleTests(unittest.TestCase):
    """模拟旅行跨日与能量变化，所有接口都在内存中执行。"""

    def setUp(self):
        self.state = "idle"
        self.daily_limit = False
        self.energy = 10
        self.has_buddy = True
        self.task_pending = False
        self.status_reply = None
        self.claim_reply = None
        self.depart_reply = None
        self.events = []
        self.remaining = 120
        self.exhaust_after_open = False
        get_patch = patch.object(signin, "get", side_effect=self.get)
        post_patch = patch.object(signin, "post", side_effect=self.post)
        get_patch.start()
        post_patch.start()
        self.addCleanup(get_patch.stop)
        self.addCleanup(post_patch.stop)

    def get(self, url, headers):
        path = url.split("/v2/activity/growth", 1)[1]
        self.events.append(("GET", path))
        if path == "/buddy/travel/status":
            if isinstance(self.status_reply, Exception):
                raise self.status_reply
            return copy.deepcopy(self.status_reply) if self.status_reply is not None else response({
                "state": self.state, "record_id": 7, "daily_limit_reached": self.daily_limit,
                "location": {"name": "咖啡馆"}, "arrive_at": 0, "server_now": 0})
        if path == "/buddy/travel/config":
            return response({"locations": [{"id": 1, "name": "咖啡馆", "duration_hours": 1}]})
        if path == "/tasks":
            return response({"tasks": [{"task_code": "first_buddy", "title": "首只 Buddy",
                                         "accept_status": "completed"}] if self.task_pending else []})
        if path == "/streak":
            return response({"streak": {"days": 1}, "makeup_cards": {"balance": 0}})
        if path == "/redeem/summary":
            return response({tier + "_status": "locked" for tier in ("starter", "advanced", "legendary")})
        if path == "/lottery/chances":
            return response({"balance": 0})
        if path == "/buddy/quota":
            return response({"affordable": self.energy // 10, "max_open_count": 1})
        if path == "/energy":
            return response({"balance": self.energy})
        self.fail("Unexpected GET: " + path)

    def post(self, url, headers, payload=None, **kwargs):
        path = url.split("/v2/activity/growth", 1)[1]
        self.events.append(("POST", path))
        if path == "/buddy/travel/claim":
            if isinstance(self.claim_reply, Exception):
                raise self.claim_reply
            if self.claim_reply is not None:
                return copy.deepcopy(self.claim_reply)
            self.state = "idle"
            return response({"reward_credit": 50})
        if path == "/tasks/first_buddy/claim":
            self.task_pending = False
            self.energy += 8
            return response({"credit": 300, "energy": 8})
        if path == "/buddy/open":
            self.energy -= payload["count"] * 10
            self.has_buddy = True
            self.state = "idle"
            if self.exhaust_after_open:
                self.remaining = 0
            return response({"buddy": "新 Buddy"})
        if path == "/buddy/travel/depart":
            if self.depart_reply is not None:
                return copy.deepcopy(self.depart_reply)
            if not self.has_buddy:
                return 403, {"msg": "no active buddy"}
            self.state = "traveling"
            self.daily_limit = True
            return response({"location": {"name": "咖啡馆"}, "duration_hours": 1})
        self.fail("Unexpected POST: " + path)

    def run_growth(self):
        signin._start_budget()
        return signin.run_growth({}, "https://test.invalid")

    def writes(self):
        return [path for method, path in self.events if method == "POST"]

    def assert_no_buddy_mutation(self):
        self.assertNotIn("/buddy/open", self.writes())
        self.assertNotIn("/buddy/travel/depart", self.writes())

    def test_traveling_keeps_trip_and_still_claims_task_rewards(self):
        self.state = "traveling"
        self.task_pending = True
        code, out = self.run_growth()
        self.assertEqual((code, out["credits_gained"]), (0, 300))
        self.assertEqual(self.state, "traveling")
        self.assert_no_buddy_mutation()

    def test_unclaimed_gift_is_preserved_on_failed_claim(self):
        self.state = "arrived"
        for reply in ((500, {"msg": "unavailable"}),
                      (200, {"code": 10001, "data": {"reward_credit": 0}}),
                      RuntimeError("synthetic claim failure")):
            with self.subTest(reply=reply):
                self.events.clear()
                self.state = "arrived"
                self.claim_reply = reply
                self.run_growth()
                self.assert_no_buddy_mutation()

    def test_unknown_travel_state_blocks_buddy_writes_but_not_other_rewards(self):
        replies = [(503, {"msg": "unavailable"}), response({}),
                   response({"state": "unexpected"}),
                   (200, {"code": 10001, "data": {"state": "idle"}}),
                   RuntimeError("synthetic status failure")]
        for reply in replies:
            with self.subTest(reply=reply):
                self.events.clear()
                self.status_reply = reply
                self.task_pending = True
                code, out = self.run_growth()
                self.assertEqual((code, out["credits_gained"]), (1, 300))
                self.assertFalse(out["idle"])
                self.assert_no_buddy_mutation()

    def test_bad_display_metadata_does_not_drop_known_travel_state(self):
        self.status_reply = response({"state": "traveling", "location": "unexpected"})
        self.run_growth()
        self.assert_no_buddy_mutation()

    def test_task_energy_is_used_before_departure_for_first_buddy(self):
        self.has_buddy = False
        self.energy = 2
        self.task_pending = True
        code, out = self.run_growth()
        self.assertEqual((code, out["credits_gained"]), (0, 300))
        self.assertEqual(self.writes(), ["/tasks/first_buddy/claim", "/buddy/open",
                                         "/buddy/travel/depart"])
        self.assertEqual(self.state, "traveling")

    def test_daily_only_runs_open_before_each_new_trip(self):
        self.energy = 40
        for day in range(4):
            with self.subTest(day=day):
                if day:
                    self.assertEqual(self.state, "traveling")
                    self.state = "arrived"
                    self.daily_limit = False
                self.events.clear()
                code, _ = self.run_growth()
                self.assertEqual(code, 0)
                expected = (["/buddy/travel/claim"] if day else []) + [
                    "/buddy/open", "/buddy/travel/depart"]
                self.assertEqual(self.writes(), expected)
                self.assertEqual(self.state, "traveling")
        self.assertEqual(self.energy, 0)

    def test_used_daily_quota_still_allows_opening(self):
        self.daily_limit = True
        code, _ = self.run_growth()
        self.assertEqual((code, self.writes()), (0, ["/buddy/open"]))
        self.assertNotIn(("GET", "/buddy/travel/config"), self.events)

    def test_insufficient_energy_does_not_block_departure(self):
        self.energy = 0
        code, _ = self.run_growth()
        self.assertEqual((code, self.writes()), (0, ["/buddy/travel/depart"]))

    def test_only_known_no_buddy_403_is_a_benign_skip(self):
        self.energy = 0
        for msg in ("no active buddy", " No Active Buddy "):
            with self.subTest(msg=msg):
                self.events.clear()
                self.depart_reply = 403, {"msg": msg}
                code, out = self.run_growth()
                self.assertEqual(code, 0)
                self.assertTrue(out["idle"])
                self.assertNotIn("failures", out)
                self.assertIn("派 Buddy 跳过", out["report"])

    def test_unknown_auth_rejections_remain_visible_and_keep_earned_credits(self):
        cases = [(403, {}), (403, {"msg": "Forbidden"}),
                 (403, {"msg": "Forbidden "}), (403, {"msg": "permission denied"}),
                 (401, {"msg": "no active buddy"})]
        self.energy = 0
        for reply in cases:
            with self.subTest(reply=reply):
                self.events.clear()
                self.task_pending = True
                self.depart_reply = reply
                code, out = self.run_growth()
                self.assertEqual(code, 1)
                self.assertEqual(out["http"], reply[0])
                self.assertTrue(out["needs_attention"])
                self.assertEqual(out["credits_gained"], 300)
                self.assertIn("首只 Buddy", out["report"])
                self.assertEqual(self.events[-1], ("POST", "/buddy/travel/depart"))

    def test_budget_exhausted_after_opening_prevents_departure(self):
        self.exhaust_after_open = True
        with patch.object(signin, "_budget_left", side_effect=lambda: self.remaining):
            _, out = self.run_growth()
        self.assertEqual(self.writes(), ["/buddy/open"])
        self.assertIn("派 Buddy 跳过", out["report"])


if __name__ == "__main__":
    unittest.main()
