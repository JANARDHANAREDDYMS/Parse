# Maximor operations console

React + TypeScript + Vite dashboard for the order-form pipeline. Without an organization ID it uses generated mock data; with an ID it uses the FastAPI read/upload APIs. Run `npm install`, then `npm run dev` (or `npm run build`).

For local API mode, optionally create `frontend/.env.local` (never commit secrets):

```bash
VITE_MAXIMOR_API_BASE_URL=http://127.0.0.1:8000
VITE_MAXIMOR_ORGANIZATION_ID=<organization-uuid>
```

The browser adapter calls `POST /v1/organizations/{organization_id}/documents`,
`GET /v1/organizations/{organization_id}/documents`, and the per-document
`/pipeline` route. It polls active documents every two seconds. The existing
mock fixtures remain the fallback when the API is unavailable.

The backend does not expose a persistent batch API; “batch” remains a
client-side group of uploads. No authentication or PDF viewer is included.
