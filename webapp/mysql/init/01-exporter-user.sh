#!/bin/bash
# Runs once, when MySQL initialises an EMPTY data volume (docker-entrypoint-initdb.d).
# Creates a least-privilege account for mysqld-exporter.
# Password comes from MYSQL_EXPORTER_PASSWORD (.env) — never hard-code it here.

if [ -z "${MYSQL_EXPORTER_PASSWORD:-}" ]; then
  echo "[init] MYSQL_EXPORTER_PASSWORD is not set" >&2
  exit 1
fi

MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" mysql --protocol=socket -uroot <<-EOSQL
  CREATE USER IF NOT EXISTS 'exporter'@'%' IDENTIFIED BY '${MYSQL_EXPORTER_PASSWORD}' WITH MAX_USER_CONNECTIONS 3;
  GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
EOSQL

echo "[init] created MySQL user 'exporter'"
