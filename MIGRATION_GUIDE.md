# ShareSentinel Production Migration Guide

Migrate ShareSentinel from a dev machine to an Ubuntu production server with all data preserved.

## Prerequisites

- Production server with Docker and Docker Compose installed
- SSH access to the production server
- GitHub access from the production server (or ability to SCP the repo)

## Step 1: Export the Database (Dev Machine)

```bash
./scripts/export_db.sh
```

Verify the export:

```bash
ls -lh backups/sharesentinel_*.sql.gz
gunzip -c backups/sharesentinel_*.sql.gz | head -50
```

## Step 2: Push Code to GitHub (Dev Machine)

```bash
git add -A
git commit -m "Add production migration scripts"
git push origin main
```

The `.gitignore` excludes `.env`, `*.pfx`, and `backups/` — only code is pushed.

## Step 3: Clone Repo on Production Server

```bash
ssh prod-server
git clone https://github.com/<your-org>/ShareSentinel.git
cd ShareSentinel
```

## Step 4: Transfer Sensitive Files (Dev Machine)

Three files must be transferred manually via SCP:

```bash
scp .env prod-server:~/ShareSentinel/.env
scp JonPnPCert.pfx prod-server:~/ShareSentinel/JonPnPCert.pfx
scp backups/sharesentinel_*.sql.gz prod-server:~/ShareSentinel/backups/
```

Create the `backups/` directory on the server first if the SCP target doesn't exist:

```bash
ssh prod-server "mkdir -p ~/ShareSentinel/backups"
```

## Step 5: Review .env on Production Server

On the production server, review `.env` and update any dev-specific values:

- `DASHBOARD_PORT` — change if port 8080 conflicts
- SMTP settings — update if the mail relay differs
- Any dev-specific hostnames or IPs

## Step 6: Start Containers

```bash
cd ~/ShareSentinel
docker compose up -d
```

Wait for all containers to be healthy:

```bash
docker compose ps
```

## Step 7: Import the Database

```bash
./scripts/import_db.sh backups/sharesentinel_YYYY-MM-DD.sql.gz
```

The script waits for Postgres to be ready, imports the dump, and prints row counts for verification.

## Step 8: Verify Everything

```bash
# All containers healthy
docker compose ps

# Worker connects to Redis and processes jobs
docker compose logs --tail=50 worker

# Audit poller is running
docker compose logs --tail=50 lifecycle-cron

# Database has your data
docker exec sharesentinel-postgres psql -U sharesentinel -d sharesentinel \
  -c "SELECT COUNT(*) FROM events; SELECT COUNT(*) FROM verdicts; SELECT COUNT(*) FROM sharing_link_lifecycle;"

# Dashboard is accessible
curl -s http://localhost:8080/api/health || echo "Dashboard not responding"
```

## Step 9: Shut Down Dev Machine

Once production is verified:

```bash
# On dev machine:
docker compose down
```

## What's NOT Migrated

- **Redis data** — Transient job queue. In-flight jobs will be reprocessed by the audit log poller.
- **DNS / reverse proxy** — Depends on your network setup.
- **SSL/TLS** — Consider adding an nginx reverse proxy for HTTPS.
- **Firewall rules** — Configure per your server's security requirements.
