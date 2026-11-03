#!/bin/bash
# TLS only: the image's snakeoil certificate, and pg_hba allowing TCP over hostssl only.
set -e
cp /etc/ssl/certs/ssl-cert-snakeoil.pem "$PGDATA/server.crt"
cp /etc/ssl/private/ssl-cert-snakeoil.key "$PGDATA/server.key"
chmod 600 "$PGDATA/server.key"
echo "ssl = on" >> "$PGDATA/postgresql.conf"
cat > "$PGDATA/pg_hba.conf" <<'HBA'
local all all trust
hostssl all all all scram-sha-256
HBA
