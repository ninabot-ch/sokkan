#!/bin/sh
# CalDAV on :5232 (htpasswd plain: alice/alicepw, bob/bobpw), WebDAV on :8080 (alice/alicepw)
set -e
mkdir -p /data/radicale /data/dav/Qualite/2026 /data/dav/Prive
printf 'alice:alicepw\nbob:bobpw\n' > /data/users
cat > /data/radicale.conf <<CONF
[server]
hosts = 0.0.0.0:5232
[auth]
type = htpasswd
htpasswd_filename = /data/users
htpasswd_encryption = plain
[storage]
filesystem_folder = /data/radicale
CONF
echo "Procédure qualité : chaque livrable est relu par deux personnes." > /data/dav/Qualite/procedure.txt
echo "Comité 2026 : décision de passer au format trimestriel." > /data/dav/Qualite/2026/comite.md
echo "privé" > /data/dav/Prive/salaires.txt
cat > /data/wsgidav.yaml <<CONF
host: 0.0.0.0
port: 8080
provider_mapping:
  "/": "/data/dav"
http_authenticator:
  domain_controller: null
  accept_basic: true
  accept_digest: false
  default_to_digest: false
simple_dc:
  user_mapping:
    "*":
      "alice":
        password: "alicepw"
verbose: 1
CONF
python -m radicale --config /data/radicale.conf &
exec wsgidav --config /data/wsgidav.yaml
