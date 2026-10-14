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
