"""plex-buddy: one page for the helpers that run next to Plex Media Server.

It has no logic of its own. It serves the page and passes the page's requests on
to two daemons on the same machine:

  plex-audio-offset   shifts the audio of what a player is playing
  plex-cc-fixer       makes subtitles and cheat sheets, and knows what is playing

  python3 -m plex_buddy.server [--port 8780]

There is no login: keep it on your local network.
"""
import argparse
import json
import os
import socketserver
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 16 * 1024

# What the page may send on, as (method, path on this server) -> (daemon, path on the daemon).
PASSED_ON = {
    ("POST", "/api/offset/offset"): ("offset", "/api/offset"),
    ("POST", "/api/offset/forget"): ("offset", "/api/forget"),
    ("POST", "/api/offset/tuning"): ("offset", "/api/tuning"),
    ("GET", "/api/recordings"): ("context", "/api/recordings"),
    ("GET", "/api/now"): ("context", "/api/now"),      # for the cheat sheet pages: is this recording playing,
    ("POST", "/api/seek"): ("context", "/api/seek"),   # and jump its playback to a part
    ("POST", "/api/jobs"): ("context", "/api/jobs"),
    ("POST", "/api/jobs/cancel"): ("context", "/api/jobs/cancel"),
}
NAMES = {"offset": "The audio offset service", "context": "The subtitle and cheat sheet service"}


class Handler(BaseHTTPRequestHandler):
    daemons = {}

    def ask(self, daemon, path, method="GET", body=None, timeout=5):
        """Send one request to a daemon. Returns (status, content type, body)."""
        request = urllib.request.Request(self.daemons[daemon] + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.headers.get("Content-Type", "application/octet-stream"), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", "application/json"), error.read()
        except (OSError, ValueError):
            return 502, "application/json", json.dumps({"error": NAMES[daemon] + " is not answering."}).encode()

    def state(self):
        """What both daemons know about what is playing; null for one that does not answer."""
        merged = {}
        for name, path in (("offset", "/api/state"), ("context", "/api/now")):
            status, _, body = self.ask(name, path, timeout=3)
            try:
                merged[name] = json.loads(body) if status == 200 else None
            except ValueError:
                merged[name] = None
        return merged

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                return self.reply(200, "text/html; charset=utf-8", f.read())
        if path == "/api/state":
            return self.reply(200, "application/json", json.dumps(self.state()).encode())
        if path.startswith("/sheets/") and path.endswith(".html"):
            # The cheat sheet pages are the context daemon's; they are shown as they come.
            return self.reply(*self.ask("context", path[len("/sheets"):], timeout=10))
        if ("GET", path) in PASSED_ON:
            daemon, target = PASSED_ON[("GET", path)]
            return self.reply(*self.ask(daemon, target, timeout=20))
        self.reply(404, "application/json", b'{"error": "not found"}')

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if ("POST", path) not in PASSED_ON:
            return self.reply(404, "application/json", b'{"error": "not found"}')
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self.reply(400, "application/json", b'{"error": "request too large"}')
        daemon, target = PASSED_ON[("POST", path)]
        self.reply(*self.ask(daemon, target, "POST", self.rfile.read(length), timeout=20))

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer looks up its own host name here, which can take seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(listen, port, offset_url, context_url):
    handler = type("BuddyHandler", (Handler,), {
        "daemons": {"offset": offset_url.rstrip("/"), "context": context_url.rstrip("/")}})
    return Server((listen, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", default="0.0.0.0", help="address to serve on")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--offset-url", default="http://127.0.0.1:8765", help="the plex-audio-offset daemon")
    parser.add_argument("--context-url", default="http://127.0.0.1:8766", help="the plex-cc-fixer daemon")
    args = parser.parse_args(argv)
    server = make_server(args.listen, args.port, args.offset_url, args.context_url)
    print("plex-buddy on http://%s:%d/" % (args.listen, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
