import http.client
import io
import json
import time
import unittest
import urllib.request
from unittest import mock

import qoder_signin as qoder


class HostnameHeaderTests(unittest.TestCase):
    """Exercise urllib/http.client encoding with synthetic credentials and sockets."""

    def setUp(self):
        self.requests = []
        self.hostname = mock.Mock(return_value="电脑-café")
        self.session = {"token": "synthetic-token", "_auth_dir": "synthetic-auth"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        patches = (
            mock.patch.object(qoder, "IS_WINDOWS", True),
            mock.patch.object(qoder, "_risk_cache", (0.0, {})),
            mock.patch.object(qoder, "_risk_cache_root", None),
            mock.patch.object(qoder, "_sensitive_values", set()),
            mock.patch.object(qoder.platform, "machine", return_value="AMD64"),
            mock.patch.object(qoder, "_umid_candidates", return_value=[]),
            mock.patch.object(qoder, "_machine_id", return_value="synthetic-machine"),
            mock.patch.object(qoder, "load_session", return_value=self.session),
            mock.patch.object(qoder.socket, "gethostname", self.hostname),
            mock.patch.object(qoder.socket, "create_connection",
                              side_effect=AssertionError("Unexpected real network")),
            mock.patch.object(http.client.HTTPSConnection, "connect", autospec=True,
                              side_effect=self.connect),
            mock.patch.object(qoder.urllib.request, "urlopen", side_effect=opener.open),
            mock.patch.dict(qoder.os.environ, {
                "QODER_TOKEN": "", "QODER_BASE_URL": "https://qoder.invalid",
            }),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def connect(self, connection):
        wire = bytearray()
        self.requests.append(wire)
        sock = mock.Mock()
        sock.sendall.side_effect = wire.extend
        sock.makefile.side_effect = lambda *args: self.reply(wire)
        connection.sock = sock

    def reply(self, wire):
        if wire.startswith(b"POST "):
            body = {"status": "CLAIMED", "benefit": {"amount": 100}}
        else:
            now = int(time.time())
            body = {"campaigns": [{
                "campaignId": "test-campaign", "campaignKey": "today",
                "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
                "startAt": now - 3600, "endAt": now + 3600,
                "benefit": {"kind": "CREDITS", "amount": 100},
            }]}
        raw = json.dumps(body).encode("utf-8")
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: "
                          + str(len(raw)).encode("ascii")
                          + b"\r\nConnection: close\r\n\r\n" + raw)

    def api(self):
        return qoder.Api(self.session["token"], "https://qoder.invalid",
                         qoder.risk_headers(self.session["_auth_dir"]))

    def assert_hostname(self, wire, hostname):
        headers = bytes(wire).split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]
        values = [line.split(b": ", 1)[1] for line in headers
                  if line.lower().startswith(b"cosy-machinehostname:")]
        self.assertEqual(values, [hostname.encode("utf-8")])

    def test_windows_hostnames_use_utf8_on_the_wire(self):
        for hostname in ("DESKTOP-01", "小林的电脑", "café-PC", "电脑-café", "ПК-01"):
            with self.subTest(hostname=hostname):
                qoder._risk_cache = (0.0, {})
                self.hostname.return_value = hostname
                code, _, error = self.api().campaigns(retry=False)
                self.assertEqual(code, 200)
                self.assertIsNone(error)
                self.assert_hostname(self.requests[-1], hostname)

    def test_cached_hostname_survives_get_and_post_without_reencoding(self):
        first = self.api()
        self.hostname.return_value = "CHANGED"
        second = self.api()
        self.hostname.assert_called_once_with()

        self.assertEqual(first.campaigns(retry=False)[0], 200)
        self.assertEqual(second.claim("test-campaign", retry=False)[0], 200)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[0].startswith(b"GET /sash/api/v1/me/campaigns "))
        self.assertTrue(self.requests[1].startswith(
            b"POST /sash/api/v1/me/campaigns/test-campaign/claim "))
        for wire in self.requests:
            self.assert_hostname(wire, "电脑-café")
        for headers in (first.risk, second.risk, qoder._risk_cache[1]):
            self.assertEqual(headers["Cosy-MachineHostname"], "电脑-café")

    def test_non_windows_requests_omit_hostname(self):
        with mock.patch.object(qoder, "IS_WINDOWS", False):
            self.assertEqual(self.api().campaigns(retry=False)[0], 200)
        self.hostname.assert_not_called()
        self.assertNotIn(b"cosy-machinehostname:", bytes(self.requests[-1]).lower())

    def test_cli_commands_accept_unicode_hostname(self):
        cases = (
            (["doctor"], "OK", 0),
            (["status"], "OK", 1),
            (["auto", "--once"], "CLAIMED", 2),
            (["auto", "--dry-run", "--once"], "DRY_RUN", 1),
            (["silent", "--once"], "CLAIMED", 2),
        )
        for argv, expected, count in cases:
            with self.subTest(argv=argv), mock.patch.object(qoder, "emit") as emit:
                self.requests.clear()
                self.assertEqual(qoder.main(argv), 0)
                out = emit.call_args[0][0]
                self.assertEqual(out["result"], expected)
                self.assertFalse(out["needs_attention"])
                self.assertEqual(len(self.requests), count)
                for wire in self.requests:
                    self.assert_hostname(wire, "电脑-café")

    def test_header_line_injection_is_still_rejected(self):
        self.hostname.return_value = "PC\r\nInjected: yes"
        with self.assertRaises(ValueError):
            self.api().campaigns(retry=False)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
