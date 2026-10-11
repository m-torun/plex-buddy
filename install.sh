#!/bin/sh
# Install plex-buddy on the machine that runs Plex. Run as root; deploy.sh does that.
set -eu

PREFIX=/opt/plex-buddy
UNIT=/etc/systemd/system/plex-buddy.service
NAME_UNIT=/etc/systemd/system/plex-buddy-name.service
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
  --name NAME         also make http://NAME/ and http://NAME.local/ open the page, on a
                      machine that runs Pi-hole (a local DNS record), Avahi (the .local
                      name) and lighttpd on port 80 (a proxy rule)
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

# Does the DNS server on this machine answer? Asked for Pi-hole's own name, which needs no upstream.
dns_answers() {
    command -v dig >/dev/null 2>&1 || return 0
    for attempt in 1 2 3 4 5; do
        dig +short +time=2 +tries=1 @127.0.0.1 pi.hole >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

# Make Pi-hole read its local records again. Every device on the network depends on that
# server, and a reload has hung it: on Pi-hole 5 (FTL 5.7), one that arrived at second 0 of
# a minute, when FTL does its once-a-minute database work, left it running but answering
# nothing. So this is called only when a record changed, stays clear of the turn of the
# minute, checks that the server still answers, and restarts it if it does not.
reload_dns() {
    second=$(date +%S)
    second=${second#0}
    if [ "$second" -ge 55 ] || [ "$second" -lt 5 ]; then
        sleep 10
    fi
    pihole restartdns reload >/dev/null 2>&1 || true
    dns_answers && return 0
    echo "DNS: the server stopped answering after the reload; restarting it" >&2
    pihole restartdns >/dev/null 2>&1 || true
    dns_answers || fail "the DNS server does not answer, also after a restart; see: systemctl status pihole-FTL"
}

# A second way to the same page that depends on no DNS server: NAME.local, announced over
# Bonjour (mDNS) by a small service next to Avahi. A device that asked another DNS server
# for NAME, and was told there is no such name, keeps that answer for a while; NAME.local
# keeps working. The machine's link-local IPv6 address is announced as well, because a
# lookup that also asks for IPv6 otherwise waits 5 s for an answer that never comes.
announce_name() {
    publish=$(command -v avahi-publish || true)
    if [ -z "$publish" ] || ! systemctl is-active --quiet avahi-daemon; then
        echo "Bonjour: no Avahi with avahi-publish here, so no http://$NAME.local/"
        return 0
    fi
    device=$(ip -o -4 addr show 2>/dev/null | awk -v a="$ADDRESS" '{ split($4, p, "/"); if (p[1] == a) print $2 }' | head -n 1)
    address6=
    [ -z "$device" ] || address6=$(ip -o -6 addr show dev "$device" scope link 2>/dev/null | awk '{ split($4, p, "/"); print p[1] }' | head -n 1)
    unit=$(cat <<EOF
[Unit]
Description=Announce $NAME.local for plex-buddy
After=avahi-daemon.service
Requires=avahi-daemon.service

[Service]
ExecStart=/bin/sh -c 'for address in $ADDRESS $address6; do $publish -a -R $NAME.local \$\$address & done; wait'
Restart=always
RestartSec=10
User=nobody

[Install]
WantedBy=multi-user.target
EOF
)
    if [ -f "$NAME_UNIT" ] && [ "$(cat "$NAME_UNIT")" = "$unit" ] && systemctl is-active --quiet plex-buddy-name.service; then
        echo "Bonjour: http://$NAME.local/ (already announced)"
        return 0
    fi
    printf '%s\n' "$unit" > "$NAME_UNIT"
    systemctl daemon-reload
    systemctl enable --quiet plex-buddy-name.service
    systemctl restart plex-buddy-name.service
    echo "Bonjour: http://$NAME.local/"
}

# The name and its proxy rule, taken out again. Used by uninstall and when the name changes.
remove_name() {
    old=$1
    if [ -f "$NAME_UNIT" ]; then
        systemctl disable --quiet --now plex-buddy-name.service || true
        rm -f "$NAME_UNIT"
        systemctl daemon-reload
    fi
    if [ -f "$PIHOLE_HOSTS" ] && grep -q "[[:space:]]$old\$" "$PIHOLE_HOSTS"; then
        sed -i "/[[:space:]]$old\$/d" "$PIHOLE_HOSTS"
        reload_dns
    fi
    if [ -f "$LIGHTTPD_EXTRA" ] && grep -q "^$START" "$LIGHTTPD_EXTRA"; then
        sed -i "/^$START/,/^$END/d" "$LIGHTTPD_EXTRA"
        systemctl restart lighttpd
    fi
}

# Both steps leave things alone when they are already as wanted, so installing again
# after a code change touches neither the DNS server nor the web server.
add_name() {
    case $NAME in
        *[!a-z0-9-]*|'') fail "the name may hold lowercase letters, digits and hyphens only" ;;
    esac
    [ -n "$ADDRESS" ] || ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$ADDRESS" ] || fail "could not tell this machine's address; pass --address"

    if [ ! -f "$PIHOLE_HOSTS" ]; then
        echo "DNS: no Pi-hole here. Add a record $NAME -> $ADDRESS on your DNS server yourself."
    elif [ "$(grep "[[:space:]]$NAME\$" "$PIHOLE_HOSTS")" = "$ADDRESS $NAME" ]; then
        echo "DNS: $NAME -> $ADDRESS (Pi-hole local record, already there)"
    else
        sed -i "/[[:space:]]$NAME\$/d" "$PIHOLE_HOSTS"
        echo "$ADDRESS $NAME" >> "$PIHOLE_HOSTS"
        reload_dns
        echo "DNS: $NAME -> $ADDRESS (Pi-hole local record)"
    fi

    block=$(cat <<EOF
$START
server.modules += ( "mod_proxy" )
\$HTTP["host"] =~ "^$NAME(\\.|:|\$)" {
    proxy.server = ( "" => ( ( "host" => "127.0.0.1", "port" => $PORT ) ) )
}
$END
EOF
)
    if [ ! -f "$LIGHTTPD_EXTRA" ] || [ ! -f "$LIGHTTPD_MAIN" ]; then
        echo "Port 80: no lighttpd here. Use http://$NAME:$PORT/, or point your own web server at port $PORT."
    elif [ "$(sed -n "/^$START/,/^$END/p" "$LIGHTTPD_EXTRA")" = "$block" ]; then
        echo "Port 80: lighttpd passes http://$NAME/ on to port $PORT (already set up)"
    else
        cp -p "$LIGHTTPD_EXTRA" "$LIGHTTPD_EXTRA.plex-buddy-backup"
        sed -i "/^$START/,/^$END/d" "$LIGHTTPD_EXTRA"
        printf '%s\n' "$block" >> "$LIGHTTPD_EXTRA"
        if lighttpd -tt -f "$LIGHTTPD_MAIN" >/dev/null 2>&1; then
            rm -f "$LIGHTTPD_EXTRA.plex-buddy-backup"
            systemctl restart lighttpd
            echo "Port 80: lighttpd passes http://$NAME/ on to port $PORT"
        else
            mv "$LIGHTTPD_EXTRA.plex-buddy-backup" "$LIGHTTPD_EXTRA"
            fail "lighttpd rejected the proxy rule; its configuration was put back"
        fi
    fi
    announce_name
    echo "$NAME" > "$PREFIX/name"
}

do_install() {
    [ -n "$PYTHON" ] || fail "python3 not found"
    case "$PORT$OFFSET_URL$CONTEXT_URL" in
        *[\|\&\\\']*) fail "unsupported character in an option" ;;
    esac
    if [ -f "$PREFIX/name" ] && [ "$(cat "$PREFIX/name")" != "$NAME" ]; then
        remove_name "$(cat "$PREFIX/name")"
        rm -f "$PREFIX/name"
    fi
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
    [ ! -f "$NAME_UNIT" ] || echo "Bonjour name: $(systemctl is-active plex-buddy-name.service 2>/dev/null || true)"
}

case $ACTION in
    install) do_install ;;
    uninstall) do_uninstall ;;
    status) do_status ;;
    *) usage; exit 2 ;;
esac
