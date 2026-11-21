#!/bin/sh
set -eu

until python -B -c '
from laowadi.db.postgres import archive_snapshot
with archive_snapshot(None) as db:
    if not db.execute("SELECT version FROM publication WHERE singleton=1").fetchone():
        raise SystemExit("Waiting for published archive")
' 2>/dev/null
do
    sleep 2
done

