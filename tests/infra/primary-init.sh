#!/bin/bash
# Let the replica connect for streaming replication.
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
