#!/usr/bin/env bash
set -euo pipefail

# Export ShareSentinel Postgres database to a compressed SQL dump.
# Run this on the dev machine before migrating to production.
#
# Usage: ./scripts/export_db.sh

CONTAINER="sharesentinel-postgres"
DB_USER="${POSTGRES_USER:-sharesentinel}"
DB_NAME="${POSTGRES_DB:-sharesentinel}"
BACKUP_DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
TIMESTAMP=$(date +%Y-%m-%d)
BACKUP_FILE="sharesentinel_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "Exporting database '${DB_NAME}' from container '${CONTAINER}'..."

docker exec "$CONTAINER" \
  pg_dump -U "$DB_USER" -d "$DB_NAME" \
    --clean --if-exists --no-owner --no-privileges \
  | gzip > "${BACKUP_DIR}/${BACKUP_FILE}"

FILE_SIZE=$(ls -lh "${BACKUP_DIR}/${BACKUP_FILE}" | awk '{print $5}')
echo "Export complete: ${BACKUP_DIR}/${BACKUP_FILE} (${FILE_SIZE})"
