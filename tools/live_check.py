#!/usr/bin/env python3
"""Check plex-buddy end to end on a real player, without anyone holding the remote.

Plays a library item on a player through Plex's remote-control protocol (the one
Plex's own apps use on each other). While it plays, it reads what the page would
show, changes the audio offset through the page's own requests, waits until the
offset service reports the change in effect, and changes it back. Then it stops
playback and restores the item's watch state and the player's saved offset.

The player's screen shows the item, with sound, for under a minute. Run it on
the Plex server. Python 3.6+, no packages needed.

  tools/live_check.py --player roku --rating-key 1234 --plex-address 192.0.2.10:32400 \\
      --launch-url http://192.0.2.20:8060/launch/13535
"""

import argparse
import itertools
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NAME = "plex-buddy-check"
LIBRARY = "com.plexapp.plugins.library"


class Plex:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def get(self, path, params=None, method="GET"):
        url = self.url + path + ("?" + urlencode(params) if params else "")
        request = Request(url, method=method, headers={
            "X-Plex-Token": self.token, "X-Plex-Client-Identifier": NAME, "X-Plex-Product": NAME})
        with urlopen(request, timeout=15) as response:
            return ET.fromstring(response.read() or b"<empty/>")

    def watch_state(self, rating_key):
        item = self.get("/library/metadata/%s" % rating_key)[0]
        return int(item.get("viewOffset") or 0), int(item.get("viewCount") or 0)

    def restore(self, rating_key, before):
        """Put resume point and watched mark back. A play count above one comes back as one."""
        library = {"identifier": LIBRARY, "key": rating_key}
        if before[0]:
            self.get("/:/progress", dict(library, time=before[0], state="stopped"))
        else:
            # Setting progress to 0 leaves the new resume point in place; clearing and re-marking does not.
            self.get("/:/unscrobble", library)
            if before[1]:
                self.get("/:/scrobble", library)


class Player:
    def __init__(self, host, port, client_id):
        self.address, self.client_id = "http://%s:%s" % (host, port), client_id
        self.ids = itertools.count(int(time.time()) % 1000000)

    def send(self, path, params, timeout=8):
        query = urlencode(dict(params, commandID=next(self.ids)))
        request = Request("%s%s?%s" % (self.address, path, query), headers={
            "X-Plex-Client-Identifier": NAME, "X-Plex-Device-Name": NAME,
            "X-Plex-Target-Client-Identifier": self.client_id})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def answers(self):
        """False when the player's app is not serving requests, as on a Roku showing its screensaver."""
        try:
            self.send("/player/timeline/poll", {"wait": 0}, timeout=4)
            return True
        except OSError:
            return False

    def video(self):
        """State and position (seconds) of the video the player has open. Asked twice: the first answer can be stale."""
        found = ("", None)
        for attempt in range(2):
            for element in ET.fromstring(self.send("/player/timeline/poll", {"wait": 0})).iter("Timeline"):
                if element.get("type") == "video":
                    position = element.get("time", "")
                    found = (element.get("state", ""), int(position) / 1000.0 if position.isdigit() else None)
            if found[0] != "stopped":
                break
            time.sleep(0.5)
        return found


class Buddy:
    """The requests the page makes."""

    def __init__(self, url):
        self.url = url.rstrip("/")

    def state(self):
        with urlopen(self.url + "/api/state", timeout=8) as response:
            return json.load(response)

    def post(self, path, body):
        request = Request(self.url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def stream(self, client_id):
        """This player's stream as (offset service's entry, context service's entry); either can be None."""
        state = self.state()
        session = next((s for s in (state["offset"] or {}).get("sessions", []) if s.get("client") == client_id), None)
        context = next((p for p in (state["context"] or {}).get("playing", []) if p.get("client") == client_id), None)
        return session, context

    def saved_offset(self, client_id):
        for client in (self.state()["offset"] or {}).get("clients", []):
            if client["id"] == client_id:
                return client["offset_ms"]
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--player", required=True, help="part of the player's name as Plex lists it")
    parser.add_argument("--rating-key", required=True, help="library item to play; pick one nobody is watching")
    parser.add_argument("--plex-address", required=True, help="address:port the player should use to reach Plex")
    parser.add_argument("--plex-url", default="http://127.0.0.1:32400")
    parser.add_argument("--token-file", default=os.path.expanduser("~/.config/plex-token"))
    parser.add_argument("--buddy-url", default="http://127.0.0.1:8780")
    parser.add_argument("--step", type=int, default=10, help="how far to move the offset and back, in ms")
    parser.add_argument("--expect-sheet", action="store_true", help="fail unless the item's cheat sheet is offered")
    parser.add_argument("--launch-url", help="URL to POST to open the player's Plex app when it does not answer, "
                                             "for a Roku: http://<roku>:8060/launch/13535")
    args = parser.parse_args()

    began = time.time()
    failures = []

    def say(text):
        print("%5.1fs  %s" % (time.time() - began, text), flush=True)

    def check(ok, text):
        say(("ok    " if ok else "FAIL  ") + text)
        if not ok:
            failures.append(text)
        return ok

    with open(args.token_file) as f:
        plex = Plex(args.plex_url, f.read().strip())
    buddy = Buddy(args.buddy_url)
    wanted = args.player.lower()

    def listed():
        return [e for e in plex.get("/clients") if wanted in e.get("name", "").lower()]

    def open_app(why):
        if not args.launch_url:
            sys.exit(why + "; pass --launch-url to open its Plex app")
        say(why + "; opening its Plex app")
        urlopen(Request(args.launch_url, data=b"", method="POST"), timeout=10).read()

    found = listed()
    if not found:
        # Plex lists a player only while its Plex app is running.
        open_app("Plex lists no player matching %r" % args.player)
        waited = time.time()
        while time.time() - waited < 60 and not found:
            time.sleep(2)
            found = listed()
        if not found:
            sys.exit("Plex still lists no player matching %r %d s after opening the app" % (args.player, time.time() - waited))
    name = found[0].get("name")
    player = Player(found[0].get("host"), found[0].get("port"), found[0].get("machineIdentifier"))
    if not player.answers():
        # A Roku behind its screensaver is listed but does not serve requests.
        open_app("%s is listed by Plex but does not answer" % name)
    waited = time.time()
    while time.time() - waited < 30 and not player.answers():
        time.sleep(1)
    if not player.answers():
        sys.exit("%s does not answer %d s after its Plex app was opened" % (name, time.time() - waited))
    if time.time() - began > 2:
        time.sleep(3)          # just opened: let the app finish starting before it is given a command
    state, _ = player.video()
    if state in ("playing", "paused", "buffering"):
        sys.exit("%s is %s something; not interrupting it" % (name, state))
    saved = buddy.saved_offset(player.client_id)
    say("player: %s, idle, saved offset %+d ms" % (name, saved))

    key = "/library/metadata/%s" % args.rating_key
    before = plex.watch_state(args.rating_key)
    server = plex.get("/identity").get("machineIdentifier")
    started = False

    def wait_for(description, condition, limit):
        """Poll the page's state until `condition(session, context)` holds. Returns seconds taken, or None."""
        since = time.time()
        while time.time() - since < limit:
            session, context = buddy.stream(player.client_id)
            if condition(session, context):
                return time.time() - since
            time.sleep(1)
        check(False, "%s within %d s" % (description, limit))
        return None

    try:
        queue = plex.get("/playQueues", {
            "type": "video", "shuffle": 0, "repeat": 0, "continuous": 0, "own": 1,
            "uri": "server://%s/%s%s" % (server, LIBRARY, key)}, method="POST")
        token = plex.get("/security/token", {"type": "delegation", "scope": "all"}).get("token")
        host, port = args.plex_address.rsplit(":", 1)
        player.send("/player/playback/playMedia", {
            "key": key, "offset": 0, "machineIdentifier": server, "address": host, "port": port,
            "protocol": "http", "type": "video", "token": token, "providerIdentifier": LIBRARY,
            "containerKey": "/playQueues/%s?window=100&own=1" % queue.get("playQueueID")})
        started = True
        waited = time.time()
        while time.time() - waited < 25 and player.video()[0] != "playing":
            time.sleep(0.5)
        if check(player.video()[0] == "playing", "playing on the player %.1f s after the command" % (time.time() - waited)):
            took = wait_for("the page shows the stream from both services", lambda s, c: s is not None and c is not None, 25)
            if took is not None:
                session, context = buddy.stream(player.client_id)
                check(True, "the page shows it after %.0f s: %s | %s | sheet: %s | offset %+d ms" % (
                    took, context["label"], session["device"], "yes" if context["sheet"] else "no", session["offset_ms"]))
                if args.expect_sheet:
                    check(bool(context["sheet"]), "the cheat sheet is offered")
                if check(session["adjustable"], "the stream can be shifted" + ("" if session["adjustable"] else ": " + session["reason"])):
                    for target in (saved + args.step, saved):
                        buddy.post("/api/offset/offset", {"session": session["id"], "offset_ms": target})
                        took = wait_for("offset %+d ms in effect" % target,
                                        lambda s, c: s is not None and s["offset_ms"] == target and not s["pending"], 45)
                        if took is not None:
                            check(player.video()[0] == "playing", "offset %+d ms in effect after %.0f s, still playing" % (target, took))
    finally:
        if started:
            state, position = player.video()
            # A playback command goes only to a player that is playing or paused and reports a position.
            if state in ("playing", "paused") and position is not None:
                player.send("/player/playback/stop", {"type": "video"})
                time.sleep(3)
            check(player.video()[0] == "stopped", "playback stopped")
            plex.restore(args.rating_key, before)
            say("watch state (resume ms, plays): before %s, now %s" % (before, plex.watch_state(args.rating_key)))
        if buddy.saved_offset(player.client_id) != saved:
            buddy.post("/api/offset/offset", {"client": player.client_id, "offset_ms": saved})
        check(buddy.saved_offset(player.client_id) == saved, "saved offset is %+d ms, as before" % saved)

    say("%d failure(s)" % len(failures) if failures else "all checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
