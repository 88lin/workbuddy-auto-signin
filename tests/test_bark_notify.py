import json
import unittest

import bark_notify


class BarkNotifyTests(unittest.TestCase):
    def test_critical_is_never_sent(self):
        calls=[]
        self.assertFalse(bark_notify.push_one('https://bark.example', 'key', 'title', 'body',
            level='critical', post_json=lambda *args: calls.append(args)))
        self.assertEqual(calls, [])

    def test_native_payload_and_valid_response(self):
        calls = []

        def post(url, payload):
            calls.append((url, payload))
            return 200, json.dumps({"code": 200})

        ok = bark_notify.push(
            {"bark_server": "https://bark.example/", "bark_key": "device-a"},
            "Qoder · 需要重新登录", "请检查登录状态。", level="timeSensitive",
            subtitle="Qoder CN · Windows", post_json=post,
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        url, payload = calls[0]
        self.assertEqual(url, "https://bark.example/push")
        self.assertEqual(payload["group"], "签到与额度")
        self.assertEqual(payload["level"], "timeSensitive")
        self.assertEqual(payload["isArchive"], 1)
        self.assertEqual(payload["subtitle"], "Qoder CN · Windows")
        self.assertNotIn("critical", payload.values())

    def test_shared_notify_interface(self):
        self.assertEqual(bark_notify.DEFAULT_GROUP, "签到与额度")

    def test_validates_json_code_not_a_substring(self):
        for status, response in (
            (200, '{"code":500,"message":"request 200 failed"}'),
            (200, "not json with code 200"),
            (500, '{"code":200}'),
        ):
            with self.subTest(status=status, response=response):
                self.assertFalse(bark_notify.push_one(
                    "https://bark.example", "key", "title", "body",
                    post_json=lambda url, payload: (status, response),
                ))

    def test_unique_devices_all_tried_after_one_failure(self):
        attempts = []

        def post(url, payload):
            attempts.append(payload["device_key"])
            if payload["device_key"] == "bad":
                raise OSError("offline")
            return 200, '{"code":200}'

        cfg = {
            "bark_key": "bad",
            "bark_keys": ["bad", "good"],
            "bark_extra": [{"key": "good", "group": "override"}, {"key": "third"}],
        }
        self.assertTrue(bark_notify.push(cfg, "title", "body", post_json=post))
        self.assertEqual(attempts, ["bad", "good", "third"])


if __name__ == "__main__":
    unittest.main()
