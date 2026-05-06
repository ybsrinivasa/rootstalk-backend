#!/usr/bin/env bash
# Daily Postgres backup with 7-day retention.
#
# Schedule via cron (one-time setup):
#   ( crontab -l 2>/dev/null;
#     echo "30 2 * * * /home/rootstalk/apps/ops/backup-db.sh >> /home/rootstalk/apps/ops/backup.log 2>&1"
#   ) | crontab -
#
# Verify with: crontab -l
#
# Backups land in ~/apps/db-backups/. Anything older than 7 days
# (mtime > 7d) is deleted automatically. Compressed gzip; expect
# 100s of KB per dump for an early-stage RootsTalk install.
#
# Restore (if needed):
#   gunzip -c ~/apps/db-backups/rootstalk-<TIMESTAMP>.sql.gz | psql ...
set -euo pipefail

BACKUP_DIR="$HOME/apps/db-backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
FILE="$BACKUP_DIR/rootstalk-$TIMESTAMP.sql.gz"

# Pull the password out of .env's DATABASE_URL so we don't have to
# keep a duplicate copy in .pgpass. Format expected:
#   DATABASE_URL=postgresql+asyncpg://rootstalk:<PASS>@localhost:5432/rootstalk
PGPASSWORD=$(grep ^DATABASE_URL "$HOME/apps/rootstalk-backend/.env" \
    | sed 's|.*://rootstalk:||; s|@localhost.*||')

PGPASSWORD="$PGPASSWORD" pg_dump \
    -h localhost -U rootstalk -d rootstalk \
    --format=plain --no-owner --no-acl \
    | gzip -9 > "$FILE"

# Retain 7 days
find "$BACKUP_DIR" -name 'rootstalk-*.sql.gz' -mtime +7 -delete

echo "Backup written: $FILE ($(du -h "$FILE" | cut -f1))"
