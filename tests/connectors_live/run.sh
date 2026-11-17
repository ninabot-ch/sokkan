#!/bin/bash
# run.sh SRC — the connectors against real protocol servers in containers (no real account):
# Greenmail (IMAP + SMTP), Radicale (CalDAV), WsgiDAV (WebDAV). Subnet 10.230.7.0/24 by default.
set -u
SRC=${1:?source dir}; NET=${NET:-ssconn-net}; SUBNET=${SUBNET:-10.230.7.0/24}
docker network inspect $NET >/dev/null 2>&1 || docker network create --subnet $SUBNET $NET >/dev/null
docker rm -f ssconn-mail ssconn-fakes >/dev/null 2>&1
docker run -d --name ssconn-mail --network $NET --memory 1g \
  -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users.login=email -Dgreenmail.users=alice:alicepw@exemple.test,bob:bobpw@exemple.test -Dgreenmail.verbose" \
  greenmail/standalone:2.1.3 >/dev/null
docker build -q -t ssconn-fakes -f $SRC/tests/connectors_live/Dockerfile.fakes $SRC/tests/connectors_live >/dev/null
docker run -d --name ssconn-fakes --network $NET --memory 1g ssconn-fakes >/dev/null
for i in $(seq 1 60); do
  docker logs ssconn-mail 2>&1 | grep -q "Started" && docker logs ssconn-fakes 2>&1 | grep -qi "serving on\|running" && break; sleep 2
done
nice -n 10 docker run --rm --network $NET --memory 6g --memory-swap 6g --user 0 -v $SRC:/src -w /src -e HOME=/tmp \
  -e SOKKAN_CONNECTORS_LIVE=1 -e LIVE_MAIL_HOST=ssconn-mail -e LIVE_CALDAV=http://ssconn-fakes:5232 \
  -e LIVE_WEBDAV=http://ssconn-fakes:8080 ssint-test:full \
  python -m pytest -q -rs -p no:cacheprovider tests/test_connectors_live.py
rc=$?
[ "${KEEP:-0}" = 1 ] || { docker rm -f ssconn-mail ssconn-fakes >/dev/null; docker network rm $NET >/dev/null; }
exit $rc
