# plex-buddy

One web page for the helpers that run next to Plex Media Server. Open it on your phone while you watch.

- **Now playing.** Each stream shows what is playing and on which player, a control to shift its audio earlier or later, and a button for that episode's cheat sheet when it has one.
- **Saved audio offsets** for the players that are not playing right now.
- **Recordings.** The newest recordings grouped by show, or filtered to one show, whether each has subtitles and a cheat sheet, and a button to make what is missing, with progress while it runs.

plex-buddy has no logic of its own. It serves the page and passes the page's requests on to two daemons on the same machine:

| Daemon | Does | Default address |
| --- | --- | --- |
| [plex-audio-offset](https://github.com/m-torun/plex-audio-offset) | shifts the audio of a stream, remembers an offset per player | `http://127.0.0.1:8765` |
| [plex-cc-fixer](https://github.com/m-torun/plex-cc-fixer) | makes subtitles and cheat sheets, knows what is playing | `http://127.0.0.1:8766` |

Either daemon can be missing or down. The page says so and keeps working with the other.

## Install

From a checkout on any machine with ssh access to the server (key login and passwordless sudo):

    ./deploy.sh user@server install --name plex

or on the server itself:

    sudo ./install.sh install --name plex

This puts the code in `/opt/plex-buddy`, runs it as the systemd service `plex-buddy` on port 8780, and, with `--name`, makes `http://plex/` and `http://plex.local/` open the page:

- On a machine that runs Pi-hole 5, it adds a local DNS record for the name. Every device that uses that Pi-hole for DNS can then resolve it.
- On a machine that runs Avahi, a second small service, `plex-buddy-name`, announces `plex.local` over Bonjour (mDNS).
- On a machine where lighttpd holds port 80 (Pi-hole's admin page does), it adds a rule that passes requests for that name on to plex-buddy. The admin page is unaffected.

Without Pi-hole, Avahi or lighttpd the installer says what is missing. The page is always reachable at `http://<server>:8780/`.

Which address to use:

- `http://plex.local/` works on Apple devices and most others without any DNS server being involved. Prefer it.
- `http://plex/` works only while a device gets its answer from the Pi-hole. If the device also has a second DNS server (many routers hand out themselves), one lookup that goes there comes back as "no such name", and the device remembers that for a while.

Installing again after a code change leaves the DNS record, the Bonjour name and the web server rule alone when they are already as wanted. Pi-hole is told to reload only when the record changes, and the installer then checks that it still answers, because every device on the network depends on it.

Other options: `--port N`, `--address IP` (what the name points to), `--offset-url URL`, `--context-url URL`.

    ./deploy.sh user@server uninstall     # removes the service, the DNS record and the rule

There is no login, and the page shows what is being watched and can start a half-hour job on the server. Keep it on your home network.

## How it fits together

    phone ──▶ plex-buddy :8780 ──▶ plex-audio-offset 127.0.0.1:8765
     (by name via port 80)       └▶ plex-cc-fixer     127.0.0.1:8766

- `GET /` is the page. `GET /api/state` returns both daemons' view of what is playing, with `null` for one that does not answer; the page pairs the two by player.
- `POST /api/offset/offset`, `/forget`, `/tuning` go to plex-audio-offset.
- `GET /api/recordings`, `POST /api/jobs` and `/api/jobs/cancel` go to plex-cc-fixer.
- `GET /api/now`, `POST /api/seek` and `POST /api/playback` go there too. The cheat sheet pages use them: each part has a Play button that jumps the playback to it while the recording is playing, and a Pause button beside it.
- `GET /sheets/<path>.html` shows a cheat sheet page from plex-cc-fixer.
- Nothing else is passed on.

It uses only the Python standard library and keeps no state, so the service runs as a throwaway user.

## Testing

    python3 -m unittest discover -s tests -t .

`tools/live_check.py` tests the whole chain on a real player: it plays an item through Plex's remote-control protocol, reads what the page shows, moves the audio offset through the page's requests and back, then stops playback and restores the watch state and the saved offset. The item shows on that player's screen, with sound, for under a minute.

    python3 tools/live_check.py --player roku --rating-key 1234 \
        --plex-address 192.0.2.10:32400 --launch-url http://192.0.2.20:8060/launch/13535

## Files on the server

| Path | What |
| --- | --- |
| `/opt/plex-buddy/` | the code, and `name` holding the installed name |
| `/etc/systemd/system/plex-buddy.service` | the service |
| `/etc/systemd/system/plex-buddy-name.service` | announces the `.local` name, when Avahi is present |
| `/etc/pihole/custom.list` | one line for the name, when Pi-hole is present |
| `/etc/lighttpd/external.conf` | the block between `# plex-buddy:start` and `# plex-buddy:end`, when lighttpd is present |
