# Security Notes

This repository is intended to be shareable without private credentials.

Do not commit:

- `.env` or any `.env.*` file except `.env.example`
- `output/` artifacts from live runs
- ZIP exports, logs, raw OpenAPI downloads, or local account reports
- API keys, bearer tokens, private keys, seed phrases, wallet private keys, or personal wallet/profile URLs

The code may include public/read-only service URLs such as the production Arena base URL and Supabase project URL. Do not hardcode credential-shaped values, including publishable API keys. Supply them through local environment variables instead.

Before publishing, run a secret scan over the shareable tree and confirm `.env`, `.venv/`, and `output/` are ignored.
