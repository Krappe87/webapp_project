# Deploying RMM Alerts with HTTPS (Let's Encrypt)

This project uses **Caddy** as the reverse proxy. Caddy obtains and renews Let's Encrypt certificates automatically—no certbot or manual steps.

## 1. Point your domain to the server

Create an **A record** (and optionally **AAAA** for IPv6) for your domain pointing to the public IP of the host running Docker:

- `rmm.yourdomain.com` → `YOUR_SERVER_IP`

Ensure **ports 80 and 443** are open on the host (firewall and any cloud security groups).

## 2. Configure environment

In `.env` (or export before `docker compose up`):

```bash
# Required for Let's Encrypt and webhook URL
DOMAIN=kraplab.work
ACME_EMAIL=kevinrappe87@gmail.com   # used for Let's Encrypt expiry notices
```

Keep your existing `DATABASE_URL`, `DATTO_*` variables as needed.

## 3. Run the stack

```bash
docker compose up -d --build
```

Caddy will:

- Listen on **80** (HTTP) and **443** (HTTPS).
- Request a certificate from Let's Encrypt for `DOMAIN` (first time may take ~30s).
- Proxy all requests to the `web` app.
- Renew certificates automatically.

Your webhook URL will be:

- **https://rmm.kraplab.work/webhook**

Use this URL in Datto RMM when configuring the webhook.

## 4. Optional: hide the app port in production

To avoid exposing the FastAPI app directly, remove the `ports` block for the `web` service in `docker-compose.yml` so only Caddy is reachable on 80/443. The app will still be reachable via Caddy.

## Local / no domain

- Use `DOMAIN=localhost` and `ACME_EMAIL=admin@localhost` (or leave unset). Caddy will serve over HTTP only on port 80.
- You can still use `http://localhost:8000` for the app if you keep the `web` ports in compose.

## Alternative: Nginx + Certbot

If you prefer Nginx instead of Caddy:

1. Add an `nginx` service that proxies to `web:8000`.
2. Use the `certbot/certbot` image or host certbot to obtain certs into a shared volume.
3. Configure nginx to use the certs for HTTPS and to serve `/.well-known/acme-challenge/` for HTTP-01 challenges.

Caddy is used here because it handles ACME and renewal inside the same container with no extra certbot step.
