# Zee.Media Apollo Research — MCP connector

Remote MCP server that lets a named teammate do Apollo people-research from
claude.ai on Danny's Apollo subscription — **no extra Apollo seat**. The Apollo
key lives only in Render env vars; every call runs on it, so it spends Danny's
API calls / credits.

Separate from `zm-reporting-mcp` by design — nothing shared, nothing touched.

## Tools
- `find_people(company_domain, titles?, seniorities?, limit?)` — FREE, masked emails.
- `reveal_contacts(person_ids)` — ~1 Apollo credit each; daily-capped.
- `check_usage()` — reveals used today vs the cap.

## Guardrails
- **Auth:** WorkOS AuthKit (OAuth + DCR, as claude.ai requires).
- **Allowlist:** `ALLOWED_EMAILS` — only those sign-ins may call the tools (fail-closed).
- **Daily reveal cap:** `DAILY_REVEAL_CAP` (default 50) so research can't drain credits.

## Deploy (Render)
1. Push to GitHub (done).
2. Render → New → Web Service → connect this repo (reads `render.yaml`).
3. Env vars: `APOLLO_API_KEY`, `AUTHKIT_DOMAIN`, `PUBLIC_URL` (= the Render URL),
   `ALLOWED_EMAILS`, `DAILY_REVEAL_CAP`.
4. Deploy. MCP endpoint: `<PUBLIC_URL>/mcp/`.

## Auth (reuses the existing Zee.Media AuthKit app)
Same WorkOS AuthKit app as the reporting connector (DCR already enabled). The
`ALLOWED_EMAILS` gate restricts who can actually use it.

## Add to claude.ai
Settings → Connectors → Add custom connector → URL `<PUBLIC_URL>/mcp/` → authorize.
