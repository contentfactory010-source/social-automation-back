# Social Automation Backend

FastAPI service for Instagram OAuth, media loading, automation storage, and Instagram webhooks.

## Setup

Requirements: Python 3.9+.

```bash
cd back
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set the Meta credentials and URLs in `.env`:

```env
INSTAGRAM_APP_ID=
INSTAGRAM_APP_SECRET=
INSTAGRAM_REDIRECT_URI=http://localhost:8000/api/auth/instagram/callback
INSTAGRAM_WEBHOOK_VERIFY_TOKEN=
FRONTEND_URL=http://localhost:5173
COOKIE_SECURE=false
```

Start the server:

```bash
.venv/bin/uvicorn main:app --reload --port 8000
```

## API

- `GET /api/auth/instagram/start` starts Instagram OAuth.
- `GET /api/auth/instagram/callback` completes OAuth and stores the long-lived token.
- `GET /api/connection`, `DELETE /api/connection` manage the connected account.
- `GET /api/posts` loads Instagram media.
- `GET /api/automations`, `PUT /api/automations/{post_id}`, `DELETE /api/automations/{post_id}` manage simple automations.
- `GET /api/automation-drafts`, `POST /api/automation-drafts` manage form automations.
- `PATCH /api/automations/{id}/enabled` toggles a simple automation.
- `PATCH /api/automation-drafts/{id}/enabled` toggles a form automation.
- `GET /api/webhooks/instagram` verifies Meta webhook configuration.
- `POST /api/webhooks/instagram` processes Instagram comments and direct messages.
- `GET /api/health` checks service availability.

Configure the webhook callback URL in Meta Developer:

```text
https://YOUR_PUBLIC_ORIGIN/api/webhooks/instagram
```

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
pytest
```

## Docker and deployment

Docker is not required for local development. The server can run directly with Uvicorn.

Docker is recommended for Cloud Run because it gives the deployment a predictable Python runtime. The included `Dockerfile` starts the service on Cloud Run's `$PORT`.

```bash
gcloud auth login
gcloud config set project YOUR_FIREBASE_PROJECT_ID
gcloud run deploy social-automation-api \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated
```

Set all production environment variables in Cloud Run. Use HTTPS URLs and `COOKIE_SECURE=true`.

The current SQLite database is local to the service filesystem. It is suitable for an initial deployment, but it is not durable across Cloud Run restarts or multiple instances. Move persistence to Firestore or Cloud SQL before relying on this in production.

## Cloudflare Python Worker deployment

The repository also contains a Cloudflare Worker entrypoint. It keeps the FastAPI routes but uses Cloudflare Fetch for Instagram requests and D1 for persistence.

Requirements: Node.js, `uv`, and a Cloudflare account with Workers and D1 enabled.

Create the production D1 database from this directory:

```bash
npx wrangler d1 create social-automation
```

Copy the returned `database_id` into `wrangler.jsonc`. Apply the schema locally while developing:

```bash
npx wrangler d1 migrations apply social-automation --local
```

Set non-secret values in `wrangler.jsonc`. Store credentials as Worker secrets:

```bash
uv run pywrangler secret put INSTAGRAM_APP_ID
uv run pywrangler secret put INSTAGRAM_APP_SECRET
uv run pywrangler secret put INSTAGRAM_WEBHOOK_VERIFY_TOKEN
```

Apply the production migration and deploy:

```bash
npx wrangler d1 migrations apply social-automation --remote
uv run pywrangler deploy
```

Run the Worker locally with its local D1 database:

```bash
uv run pywrangler dev
```

For GitHub deployment, connect this repository in **Cloudflare Dashboard → Workers & Pages → your Worker → Settings → Builds**. Use the repository root as the build directory and:

```text
Build command: uv sync --dev
Deploy command: uv run pywrangler deploy
```

Configure the same Worker secrets in the Cloudflare dashboard. Configure the Instagram OAuth callback and webhook URLs using the deployed Worker hostname. Python Workers require the `python_workers` compatibility flag and use the `pywrangler` tool for local development and deployment. D1 is exposed through a Worker binding named `DB`.[Cloudflare Python Workers](https://developers.cloudflare.com/workers/languages/python/), [D1 from Python Workers](https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/)
