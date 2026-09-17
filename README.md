# Multi Search Gateway

Unified API gateway for **Tavily**, **Exa**, and **Brave Search** with provider routing, multi-credential management, quota-aware failover, rate limiting, and an admin UI.

## Phase 1

- HTTP API only (`POST /v1/search`)
- Providers: Tavily, Exa, Brave
- `provider=auto|tavily|exa|brave`
- Provider/account/credential abstraction
- Automatic fallback on rate limit, quota exhaustion, timeout, and provider 5xx
- Client API keys (`sk_live_*`)
- Redis-backed rate-limit/cooldown hooks
- PostgreSQL-ready persistence layer
- Admin UI skeleton
- Docker Compose for local development

> Use provider credentials only for accounts/teams you own or are authorized to use. Do not use credential rotation to bypass provider account, billing, or service limits.

## Architecture

```text
Client
  |
  v
FastAPI Gateway
  |- API key auth
  |- client rate limit
  |- routing policy
  |- provider/account/credential selector
  |- quota + cooldown checks
  |- automatic failover
  v
Tavily / Exa / Brave
  |
  v
Normalized response
```

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000`

Admin UI: `http://localhost:3000`

OpenAPI: `http://localhost:8000/docs`

## Example

```bash
curl http://localhost:8000/v1/search \
  -H 'Authorization: Bearer sk_dev_local' \
  -H 'Content-Type: application/json' \
  -d '{"query":"latest AI infrastructure news","provider":"auto","limit":10}'
```

## Provider endpoints used

- Tavily: `POST https://api.tavily.com/search` with `Authorization: Bearer ...`
- Exa: `POST https://api.exa.ai/search` with `x-api-key: ...`
- Brave: `POST https://api.search.brave.com/res/v1/web/search` with `X-Subscription-Token: ...`

## Status

Initial MVP scaffold. Persistence-backed admin CRUD, encrypted credential storage, quota sync, and production authentication are the next milestones.
