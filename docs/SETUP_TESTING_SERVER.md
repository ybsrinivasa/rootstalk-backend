# RootsTalk Testing-Server Provisioning

End-to-end runbook for bringing up the RootsTalk testing environment on a clean Ubuntu server. Last updated 2026-05-06; reflects the URL/branding/email work landed in commits 5034640, 915beee, 556113b, and b08167d.

## Architecture summary

Four apps, four hostnames, all single-label under `eywa.farm` so the existing `*.eywa.farm` wildcard cert covers them.

| Subdomain | Serves | Port (localhost) |
|---|---|---|
| `rstalk.eywa.farm` | SA admin (`rootstalk-frontend`) | 3002 |
| `rstalk-ca.eywa.farm` | CA portal (`rootstalk-client-portal`) | 3004 |
| `rstalk-pwa.eywa.farm` | PWA (`rootstalk-pwa`) | 3003 |
| `rstalkapi.eywa.farm` | FastAPI (`rootstalk-backend`) | 8001 |

Caddy on the box terminates TLS, applies the wildcard cert, and reverse-proxies each hostname to the right localhost port. See `scripts/ops/Caddyfile.testing` for the exact config.

## Prerequisites

- Ubuntu 24.04 or 22.04 LTS, root SSH access, public IP noted as `<SERVER_IP>`.
- DNS A records — all pointing at `<SERVER_IP>`:
  - `rstalk.eywa.farm`
  - `rstalk-ca.eywa.farm`
  - `rstalk-pwa.eywa.farm`
  - `rstalkapi.eywa.farm`
- The wildcard cert for `*.eywa.farm` is available to Caddy. If you have a DNS-provider API token (Cloudflare, Route53, etc.), Caddy can fetch certs automatically via DNS-01; otherwise Caddy will use HTTP-01 (works as long as ports 80/443 are reachable from Let's Encrypt).

## Step 1 — Harden the box

```bash
ssh root@<SERVER_IP>

adduser --disabled-password --gecos "" rootstalk
usermod -aG sudo rootstalk

# Copy your SSH key so you can log in directly as `rootstalk`
mkdir -p /home/rootstalk/.ssh
cp ~/.ssh/authorized_keys /home/rootstalk/.ssh/
chown -R rootstalk:rootstalk /home/rootstalk/.ssh
chmod 700 /home/rootstalk/.ssh
chmod 600 /home/rootstalk/.ssh/authorized_keys

# Disable root SSH and password auth (key-only)
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# UFW firewall
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# Auto-update security patches
apt update && apt install -y unattended-upgrades
dpkg-reconfigure --priority=low unattended-upgrades
```

**Test that key auth works as `rootstalk` BEFORE closing the root session.** Keep root open in another terminal until you've confirmed `ssh rootstalk@<SERVER_IP>` succeeds. Then log out of root permanently.

## Step 2 — Install system packages

As `rootstalk`:

```bash
sudo apt update && sudo apt upgrade -y

# Backend stack
sudo apt install -y python3.11 python3.11-venv python3-pip build-essential libpq-dev

# Frontend stack
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Database
sudo apt install -y postgresql postgresql-contrib

# Reverse proxy + auto-SSL
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Tooling
sudo apt install -y git tmux htop jq logrotate
```

Verify:

```bash
python3.11 --version    # 3.11.x
node --version          # v20.x
psql --version          # 16.x
caddy version           # v2.x
```

## Step 3 — Postgres

```bash
DB_PASS=$(openssl rand -base64 32 | tr -d "=+/")
echo "DB_PASS=$DB_PASS"
# COPY THE OUTPUT TO YOUR PASSWORD MANAGER NOW

sudo -u postgres psql <<EOF
CREATE USER rootstalk WITH PASSWORD '$DB_PASS';
CREATE DATABASE rootstalk OWNER rootstalk;
GRANT ALL PRIVILEGES ON DATABASE rootstalk TO rootstalk;
EOF
```

## Step 4 — Clone repos and install dependencies

```bash
mkdir -p ~/apps
cd ~/apps

git clone https://github.com/ybsrinivasa/rootstalk-backend.git
git clone https://github.com/ybsrinivasa/rootstalk-frontend.git
git clone https://github.com/ybsrinivasa/rootstalk-client-portal.git
git clone https://github.com/ybsrinivasa/rootstalk-pwa.git

# Backend deps
cd ~/apps/rootstalk-backend
python3.11 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# Frontend builds
for app in rootstalk-frontend rootstalk-client-portal rootstalk-pwa; do
    cd ~/apps/$app
    npm ci
    npm run build
done
```

## Step 5 — Configure `.env`

Backend `.env` at `~/apps/rootstalk-backend/.env`:

```
ENVIRONMENT=staging

# Database
DATABASE_URL=postgresql+asyncpg://rootstalk:<DB_PASS>@localhost:5432/rootstalk

# JWT
JWT_SECRET=<run: openssl rand -base64 64 | tr -d "=" — paste output>
JWT_ALGORITHM=HS256

# Super Admin
SA_EMAIL=yb@eywa.farm
SA_PASSWORD=<your strong SA password>

# Frontend / CORS — non-dev REQUIRES FRONTEND_BASE_URL or the API
# refuses to boot (see app/main.py).
FRONTEND_BASE_URL=https://rstalk.eywa.farm
ALLOWED_ORIGINS=https://rstalk.eywa.farm,https://rstalk-ca.eywa.farm,https://rstalk-pwa.eywa.farm

# Email (Gmail SMTP)
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USER=no-reply@eywa.farm
EMAIL_SMTP_PASS=<gmail app password — no spaces>
EMAIL_FROM=no-reply@eywa.farm

# SMS (Draft4SMS) — leave empty if not yet ready
DRAFT_SMS_KEY=
DRAFT_SMS_SENDER_ID=EYFARM

# Razorpay (test keys)
RAZORPAY_KEY_ID_TEST=<rzp_test_...>
RAZORPAY_KEY_SECRET_TEST=<test secret>
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=

# FCM — leave unset; service degrades gracefully when GOOGLE_APPLICATION_CREDENTIALS is empty
GOOGLE_APPLICATION_CREDENTIALS=
```

Verify SMTP works in isolation BEFORE first boot:

```bash
cd ~/apps/rootstalk-backend
venv/bin/python scripts/check_smtp.py
```

For the three Next.js apps, set `NEXT_PUBLIC_API_BASE_URL=https://rstalkapi.eywa.farm` in each app's `.env.production` (or wherever the frontend repo expects environment config; check each repo's `.env.example`).

## Step 6 — systemd services

Copy the unit files from this repo:

```bash
sudo cp ~/apps/rootstalk-backend/scripts/ops/systemd/rootstalk-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rootstalk-backend rootstalk-frontend rootstalk-client-portal rootstalk-pwa
```

Don't start them yet — first run migrations (Step 7), then bring up Caddy (Step 8).

## Step 7 — First boot

```bash
cd ~/apps/rootstalk-backend
venv/bin/alembic upgrade head

# Start services
sudo systemctl start rootstalk-backend rootstalk-frontend rootstalk-client-portal rootstalk-pwa

# Local sanity check
curl http://localhost:8001/health
# Expected: {"status":"ok","service":"rootstalk-api"}
```

## Step 8 — Caddy reverse proxy

```bash
sudo cp ~/apps/rootstalk-backend/scripts/ops/Caddyfile.testing /etc/caddy/Caddyfile
sudo systemctl reload caddy

# Smoke test from your laptop:
# curl https://rstalkapi.eywa.farm/health
```

## Step 9 — Helper scripts and backups

The four ops scripts live in this repo at `scripts/ops/`. Symlink them into `~/apps/ops/` so you have shorter paths to type:

```bash
mkdir -p ~/apps/ops
cd ~/apps/ops
for s in deploy.sh logs.sh backup-db.sh health.sh; do
    ln -sf ~/apps/rootstalk-backend/scripts/ops/$s .
done
```

Schedule the daily backup:

```bash
( crontab -l 2>/dev/null;
  echo "30 2 * * * /home/rootstalk/apps/ops/backup-db.sh >> /home/rootstalk/apps/ops/backup.log 2>&1"
) | crontab -
crontab -l
```

## Step 10 — End-to-end smoke

```bash
~/apps/ops/health.sh
```

Then in a browser:

1. `https://rstalk.eywa.farm` — SA login screen. Sign in as `yb@eywa.farm` with your SA password.
2. From SA, initiate a test client (e.g. short_name `khaza`). Confirm the onboarding email arrives at the CA's address.
3. CA clicks the link, completes onboarding, gets approved by SA. Confirm the credentials email arrives with the URL `https://rstalk.eywa.farm/login/khaza`.
4. CA visits that URL — the page should show Khaza's branding (after the frontend `[shortName]` route is deployed). Logs in.
5. CA creates a Subject Expert. Confirm the SE receives a welcome email with credentials.

## Daily operations

| Task | Command |
|---|---|
| Update one app | `~/apps/ops/deploy.sh <backend\|frontend\|client-portal\|pwa>` |
| Tail a service's logs | `~/apps/ops/logs.sh <repo>` |
| Quick health check | `~/apps/ops/health.sh` |
| Manual DB backup | `~/apps/ops/backup-db.sh` |
| List backups | `ls -lh ~/apps/db-backups/` |

Caddy access logs (HTTP requests) live at `/var/log/caddy/<host>-access.log` with 7-day retention.

## Production differences

For production, the same playbook applies with these substitutions:

- DNS: `rootstalk.eywa.farm` (SA + CA), `rootstalkapi.eywa.farm` (API), `rootstalk.in` (PWA — separate root domain, **needs its own Let's Encrypt cert** since it's not under `*.eywa.farm`).
- `.env` on the production API box:
  ```
  ENVIRONMENT=production
  FRONTEND_BASE_URL=https://rootstalk.eywa.farm
  ALLOWED_ORIGINS=https://rootstalk.eywa.farm,https://rootstalk.in
  ```
- Razorpay keys: live, not test.
- A second Caddyfile block for `rootstalk.in` (PWA) so Caddy issues a single-host cert for it.

## When something breaks

- API 500 / refuses to start → `~/apps/ops/logs.sh backend` and look for the most recent `ERROR` line. Common cause: missing or stale env var.
- API 503 on email-OTP → `~/apps/ops/logs.sh backend` will show `Email send failed to ...: <reason>`. Use `venv/bin/python scripts/check_smtp.py` to test SMTP creds in isolation.
- Frontend white-screen → `~/apps/ops/logs.sh <frontend|client-portal|pwa>` for npm/Node errors; usually a missing env var or a stale build.
- DB connection refused → `sudo systemctl status postgresql`. Check that `DATABASE_URL` matches the password set in Step 3.
- HTTPS fails / cert error → `sudo journalctl -u caddy -n 200` and verify DNS A records point at this server.

## Reference

- `scripts/ops/deploy.sh` — pull + build + restart per repo.
- `scripts/ops/logs.sh` — tail a service's journal.
- `scripts/ops/backup-db.sh` — daily Postgres backup, 7-day retention.
- `scripts/ops/health.sh` — services + public endpoints.
- `scripts/ops/Caddyfile.testing` — reference reverse-proxy config for the testing server.
- `scripts/ops/systemd/*.service` — unit files for the four apps.
- `scripts/check_smtp.py` — standalone SMTP diagnostic.
- `scripts/backfill_clientcrop_snapshots.py` — one-shot backfill for the CCA Step 1 / Batch 1B snapshot fields. Run after `alembic upgrade head` if there are existing client-crop rows pre-snapshot.
