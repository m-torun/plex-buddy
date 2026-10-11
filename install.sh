#!/bin/sh
# Install plex-buddy on the machine that runs Plex. Run as root; deploy.sh does that.
set -eu

PREFIX=/opt/plex-buddy
UNIT=/etc/systemd/system/plex-buddy.service
PORT=8780
OFFSET_URL=http://127.0.0.1:8765
CONTEXT_URL=http://127.0.0.1:8766
NAME=
ADDRESS=
PYTHON=$(command -v python3 || true)
PIHOLE_HOSTS=/etc/pihole/custom.list
LIGHTTPD_EXTRA=/etc/lighttpd/external.conf
LIGHTTPD_MAIN=/etc/lighttpd/lighttpd.conf
START='# plex-buddy:start'
END='# plex-buddy:end'
SRC=$(cd "$(dirname "$0")" && pwd)

usage() {
    cat <<EOF
usage: install.sh install|uninstall|status [options]

  --port N            port the page is served on (default $PORT)
  --name NAME         also make http://NAME/ open the page, on a machine that runs
                      Pi-hole (a local DNS record) and lighttpd on port 80 (a proxy rule)
  --address IP        address the name should point to (default: this machine's first)
  --offset-url URL    the plex-audio-offset daemon (default $OFFSET_URL)
  --context-url URL   the plex-cc-fixer daemon (default $CONTEXT_URL)
EOF
}

fail() { echo "install.sh: $*" >&2; exit 1; }

ACTION=${1:-}
[ -n "$ACTION" ] || { usage; exit 2; }
shift
while [ $# -gt 0 ]; do
    case $1 in
        --port) PORT=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --address) ADDRESS=$2; shift 2 ;;
        --offset-url) OFFSET_URL=$2; shift 2 ;;
        --context-url) CONTEXT_URL=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option $1" ;;
    esac
done

# The name and its proxy rule, taken out again. Used by uninstall and before every install.
remove_name() {
    old=$1
    if [ -f "$PIHOLE_HOSTS" ] && grep -q "[[:space:]]$old\$" "$PIHOLE_HOSTS"; then
        sed -i "/[[:space:]]$old\$/d" "$PIHOLE_HOSTS"
        pihole restartdns reload >/dev/null 2>&1 || true
    fi
    if [ -f "$LIGHTTPD_EXTRA" ] && grep -q "^$START" "$LIGHTTPD_EXTRA"; then
        sed -i "/^$START/,/^$END/d" "$LIGHTTPD_EXTRA"
        systemctl restart lighttpd
    fi
}

add_name() {
    case $NAME in
        *[!a-z0-9-]*|'') fail "the name may hold lowercase letters, digits and hyphens only" ;;
    esac
    [ -n "$ADDRESS" ] || ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$ADDRESS" ] || fail "could not tell this machine's address; pass --address"

    if [ -f "$PIHOLE_HOSTS" ]; then
        sed -i "/[[:space:]]$NAME\$/d" "$PIHOLE_HOSTS"
        echo "$ADDRESS $NAME" >> "$PIHOLE_HOSTS"
        pihole restartdns reload >/dev/null
        echo "DNS: $NAME -> $ADDRESS (Pi-hole local record)"
    else
        echo "DNS: no Pi-hole here. Add a record $NAME -> $ADDRESS on your DNS server yourself."
    fi

    if [ -f "$LIGHTTPD_EXTRA" ] && [ -f "$LIGHTTPD_MAIN" ]; then
        cp -p "$LIGHTTPD_EXTRA" "$LIGHTTPD_EXTRA.plex-buddy-backup"
        sed -i "/^$START/,/^$END/d" "$LIGHTTPD_EXTRA"
        cat >> "$LIGHTTPD_EXTRA" <<EOF
$START
server.modules += ( "mod_proxy" )
\$HTTP["host"] =~ "^$NAME(\\.|:|\$)" {
    proxy.server = ( "" => ( ( "host" => "127.0.0.1", "port" => $PORT ) ) )
}
$END
EOF
        if lighttpd -tt -f "$LIGHTTPD_MAIN" >/dev/null 2>&1; then
            rm -f "$LIGHTTPD_EXTRA.plex-buddy-backup"
            systemctl restart lighttpd
            echo "Port 80: lighttpd passes http://$NAME/ on to port $PORT"
        else
            mv "$LIGHTTPD_EXTRA.plex-buddy-backup" "$LIGHTTPD_EXTRA"
            fail "lighttpd rejected the proxy rule; its configuration was put back"
        fi
    else
        echo "Port 80: no lighttpd here. Use http://$NAME:$PORT/, or point your own web server at port $PORT."
    fi
    echo "$NAME" > "$PREFIX/name"
}

do_install() {
    [ -n "$PYTHON" ] || fail "python3 not found"
    case "$PORT$OFFSET_URL$CONTEXT_URL" in
        *[\|\&\\\']*) fail "unsupported character in an option" ;;
    esac
    [ -f "$PREFIX/name" ] && remove_name "$(cat "$PREFIX/name")" && rm -f "$PREFIX/name"
    install -d -m 755 "$PREFIX/plex_buddy/web"
    install -m 644 "$SRC"/plex_buddy/*.py "$PREFIX/plex_buddy/"
    install -m 644 "$SRC/plex_buddy/web/index.html" "$PREFIX/plex_buddy/web/"
    sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@PYTHON@|$PYTHON|g" -e "s|@PORT@|$PORT|g" \
        -e "s|@OFFSET_URL@|$OFFSET_URL|g" -e "s|@CONTEXT_URL@|$CONTEXT_URL|g" \
        "$SRC/packaging/plex-buddy.service.in" > "$UNIT"
    systemctl daemon-reload
    systemctl enable --quiet plex-buddy.service
    systemctl restart plex-buddy.service
    [ -z "$NAME" ] || add_name
    echo "plex-buddy is at http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/${NAME:+ and http://$NAME/}"
}

do_uninstall() {
    [ -f "$PREFIX/name" ] && remove_name "$(cat "$PREFIX/name")"
    if [ -f "$UNIT" ]; then
        systemctl disable --quiet --now plex-buddy.service || true
        rm -f "$UNIT"
        systemctl daemon-reload
    fi
    rm -rf "$PREFIX"
    echo "plex-buddy removed. The two daemons it talked to are untouched."
}

do_status() {
    echo "service: $(systemctl is-active plex-buddy.service 2>/dev/null || true)"
    [ -f "$PREFIX/name" ] && echo "name: $(cat "$PREFIX/name")" || echo "name: none"
}

case $ACTION in
    install) do_install ;;
    uninstall) do_uninstall ;;
    status) do_status ;;
    *) usage; exit 2 ;;
esac
