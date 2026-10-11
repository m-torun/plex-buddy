"""The page's requests reach the right daemon, and a daemon that is down does not take the page with it."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from plex_buddy import server


class Fake(BaseHTTPRequestHandler):
    """A stand-in daemon that answers from a table and remembers what it was sent."""
    answers = {}
    seen = []

    def respond(self, body=None):
        self.seen.append((self.command, self.path, body))
        status, content_type, payload = self.answers.get((self.command, self.path), (404, "application/json", b'{"error": "nope"}'))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

    def log_message(self, format, *args):
        pass


def fake(answers):
    handler = type("FakeDaemon", (Fake,), {"answers": answers, "seen": []})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, handler


class BuddyTest(unittest.TestCase):
    def setUp(self):
        self.offset, self.offset_seen = fake({
            ("GET", "/api/state"): (200, "application/json", b'{"sessions": [], "clients": [], "plex_ok": true, "max_offset_ms": 2000}'),
            ("POST", "/api/offset"): (200, "application/json", b'{"offset_ms": 120}'),
            ("POST", "/api/forget"): (404, "application/json", b'{"error": "unknown player"}'),
        })
        self.context, self.context_seen = fake({
            ("GET", "/api/now"): (200, "application/json", b'{"playing": [], "error": null}'),
            ("GET", "/api/recordings"): (200, "application/json", b'{"recordings": [], "error": null}'),
            ("POST", "/api/jobs"): (200, "application/json", b'{"id": 7, "state": "queued"}'),
            ("POST", "/api/seek"): (200, "application/json", b'{"player": "TV", "position": 61.0}'),
            ("GET", "/Show/Season%201/Ep%3B%20one.html"): (200, "text/html; charset=utf-8", b"<h1>sheet</h1>"),
        })
        self.start_buddy("http://127.0.0.1:%d" % self.offset.server_port)

    def start_buddy(self, offset_url):
        self.buddy = server.make_server("127.0.0.1", 0, offset_url, "http://127.0.0.1:%d/" % self.context.server_port)
        threading.Thread(target=self.buddy.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.buddy.server_port

    def tearDown(self):
        for httpd in (self.buddy, self.offset, self.context):
            httpd.shutdown()
            httpd.server_close()

    def call(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=body, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type"), error.read()

    def test_page(self):
        status, content_type, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"Plex buddy", body)

    def test_state_joins_both_daemons(self):
        status, _, body = self.call("/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state["offset"]["max_offset_ms"], 2000)
        self.assertEqual(state["context"], {"playing": [], "error": None})

    def test_a_daemon_that_is_down_is_null_not_an_error(self):
        self.buddy.shutdown()
        self.buddy.server_close()
        self.start_buddy("http://127.0.0.1:9")  # nothing listens on the discard port
        status, _, body = self.call("/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(state["offset"])
        self.assertEqual(state["context"]["playing"], [])
        status, _, body = self.call("/api/offset/offset", b'{"session": "x", "offset_ms": 10}')
        self.assertEqual(status, 502)
        self.assertIn("audio offset service", json.loads(body)["error"])

    def test_offset_requests_go_to_the_offset_daemon_unchanged(self):
        status, _, body = self.call("/api/offset/offset", b'{"session": "abc", "offset_ms": 120}')
        self.assertEqual((status, json.loads(body)), (200, {"offset_ms": 120}))
        self.assertEqual(self.offset_seen.seen[-1], ("POST", "/api/offset", b'{"session": "abc", "offset_ms": 120}'))

    def test_a_daemons_own_error_comes_back_as_it_was(self):
        status, _, body = self.call("/api/offset/forget", b'{"client": "nobody"}')
        self.assertEqual((status, json.loads(body)), (404, {"error": "unknown player"}))

    def test_jobs_and_recordings_go_to_the_context_daemon(self):
        self.assertEqual(self.call("/api/recordings")[0], 200)
        status, _, body = self.call("/api/jobs", b'{"key": "2213"}')
        self.assertEqual((status, json.loads(body)["state"]), (200, "queued"))
        self.assertEqual(self.context_seen.seen[-1], ("POST", "/api/jobs", b'{"key": "2213"}'))

    def test_a_sheet_pages_requests_go_to_the_context_daemon(self):
        self.assertEqual(self.call("/api/now")[0], 200)
        seek = b'{"key": "2213", "client": "abc", "position": 61}'
        self.assertEqual(self.call("/api/seek", seek)[0], 200)
        self.assertEqual(self.context_seen.seen[-1], ("POST", "/api/seek", seek))

    def test_sheet_pages_come_from_the_context_daemon(self):
        status, content_type, body = self.call("/sheets/Show/Season%201/Ep%3B%20one.html")
        self.assertEqual((status, body), (200, b"<h1>sheet</h1>"))
        self.assertIn("text/html", content_type)

    def test_only_listed_requests_are_passed_on(self):
        self.assertEqual(self.call("/sheets/api/jobs")[0], 404)       # not a page
        self.assertEqual(self.call("/api/offset/state")[0], 404)      # not on the list
        self.assertEqual(self.call("/api/anything", b"{}")[0], 404)
        self.assertEqual(self.call("/api/jobs", b"x" * (server.MAX_BODY + 1))[0], 400)


if __name__ == "__main__":
    unittest.main()
