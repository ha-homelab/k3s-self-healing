#!/usr/bin/env bash
# Run in an official MySQL client container against a deliberately EMPTY schema.
# Credentials belong in a private [client] option file, never argv or this repo.
set -euo pipefail
if [ "$#" -ne 4 ]; then
  echo 'Usage: import_database.sh CLIENT_CNF VERIFIED_RECORDER_SQL EMPTY_DATABASE EXPECTED_TABLE_COUNT' >&2
  exit 2
fi
client_cnf=$1
recorder_dump=$2
restore_database=$3
expected_tables=$4
if [[ ! "$restore_database" =~ ^[A-Za-z0-9_]+$ ]] || [[ ! "$expected_tables" =~ ^[1-9][0-9]*$ ]]; then
  echo 'Invalid database or table count' >&2
  exit 2
fi
if [ ! -f "$client_cnf" ] || [ -L "$client_cnf" ] || [ ! -f "$recorder_dump" ] || [ -L "$recorder_dump" ]; then
  echo 'Regular private credentials and a verified dump are required' >&2
  exit 2
fi
permissions=$(stat -c '%a' "$client_cnf")
if (( (8#$permissions & 077) != 0 )); then
  echo 'Client credentials must be private (mode 0600 or 0400)' >&2
  exit 2
fi
# Opening the database must succeed: this script does not create/drop databases.
tables=$(mysql --defaults-extra-file="$client_cnf" --database="$restore_database" --batch --skip-column-names \
  -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')
if [ "$tables" != 0 ]; then
  echo 'Refusing to import into a non-empty database' >&2
  exit 1
fi
case "${recorder_dump##*/}" in
  recorder.sql.gz) decoder=(gzip) ;;
  recorder-*.sql.xz)
    if [[ ! "${recorder_dump##*/}" =~ ^recorder-[0-9a-f]{64}\.sql\.xz$ ]]; then
      echo 'Unsupported recorder generation name' >&2; exit 2
    fi
    decoder=(xz --format=xz) ;;
  *) echo 'Unsupported recorder dump format' >&2; exit 2 ;;
esac
"${decoder[@]}" -t "$recorder_dump"
"${decoder[@]}" -dc "$recorder_dump" | mysql --defaults-extra-file="$client_cnf" --database="$restore_database" --binary-mode
actual=$(mysql --defaults-extra-file="$client_cnf" --database="$restore_database" --batch --skip-column-names \
  -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')
if [ "$actual" != "$expected_tables" ]; then
  echo 'Imported table count differs from the verified archive manifest' >&2
  exit 1
fi
printf 'Recorder imported successfully: %s tables. Home Assistant remains stopped.\n' "$actual"
