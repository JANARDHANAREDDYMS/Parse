# Maximor operations console

React + TypeScript + Vite dashboard for the order-form pipeline. The dashboard starts empty and loads tenant data from FastAPI after an organization ID is supplied. Run `npm install`, then `npm run dev` (or `npm run build`).

For local API mode, optionally create `frontend/.env.local` (never commit secrets):

```bash
VITE_MAXIMOR_API_BASE_URL=http://127.0.0.1:8002
VITE_MAXIMOR_ORGANIZATION_ID=<organization-uuid>
```

The browser adapter calls `POST /v1/organizations/{organization_id}/documents`,
`GET /v1/organizations/{organization_id}/documents`, and the per-document
`/pipeline` route. It polls active documents every two seconds. When the API is
unavailable, the dashboard shows an explicit unavailable state.

The backend does not expose a persistent batch API; “batch” remains a
client-side group of uploads. No authentication or PDF viewer is included.
