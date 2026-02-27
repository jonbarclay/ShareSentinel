#!/usr/bin/env bash
set -euo pipefail

# Import a ShareSentinel Postgres database dump into the running container.
# Run this on the production server after starting containers.
#
# Usage: ./scripts/import_db.sh backups/sharesentinel_YYYY-MM-DD.sql.gz

CONTAINER="sharesentinel-postgres"
DB_USER="${POSTGRES_USER:-sharesentinel}"
DB_NAME="${POSTGRES_DB:-sharesentinel}"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <backup-file.sql.gz>"
  exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
  echo "Error: File not found: $BACKUP_FILE"
  exit 1
fi

# Wait for Postgres to be ready
echo "Waiting for Postgres container to be ready..."
MAX_WAIT=60
WAITED=0
until docker exec "$CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" -q 2>/dev/null; do
  WAITED=$((WAITED + 2))
  if [ "$WAITED" -ge "$MAX_WAIT" ]; then
    echo "Error: Postgres not ready after ${MAX_WAIT}s. Is the container running?"
    exit 1
  fi
  sleep 2
done
echo "Postgres is ready."

# Copy dump into container and import
echo "Importing ${BACKUP_FILE}..."
gunzip -c "$BACKUP_FILE" | docker exec -i "$CONTAINER" \
  psql -U "$DB_USER" -d "$DB_NAME" --single-transaction -q

echo ""
echo "Import complete. Verifying row counts:"
echo "----------------------------------------"

docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "
SELECT 'events' AS table_name, COUNT(*) FROM events
UNION ALL SELECT 'verdicts', COUNT(*) FROM verdicts
UNION ALL SELECT 'sharing_link_lifecycle', COUNT(*) FROM sharing_link_lifecycle
UNION ALL SELECT 'audit_poll_state', COUNT(*) FROM audit_poll_state
ORDER BY table_name;
"

echo "Done. Review the counts above to confirm data was imported correctly."
