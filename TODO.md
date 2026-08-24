# TODO / Future Work

## Production Deployment

Steps to make divvy-forge a live, publicly accessible application:

- [ ] Switch TrueForge to production mode — Docker Compose with PostgreSQL
  (persistent storage) and Redis (executor peering) instead of local SQLite.
- [ ] Set production env vars on the TrueForge server: `NODE_ENV=production`,
  `STANDALONE=false`, `REDIS_URL`, `PUBLIC_BASE_URL`, `OPENAI_API_KEY`.
- [ ] Expose MCP servers as HTTP servers — currently `mcp+stdio://` (local
  subprocess). For production, host them on a VPS/Railway/Fly.io and register
  with HTTP URLs instead of stdio paths.
- [ ] Point `TRUEFORGE_BASE_URL` in divvy-forge's `.env` to the production URL.
- [ ] Replace the personal GitHub token with a GitHub App for production use.
