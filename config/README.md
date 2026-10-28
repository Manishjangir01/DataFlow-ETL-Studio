# config/

Runtime configuration files created by the application.

- `secret.key` - generated automatically on first start **only if** `DATAFLOW_SECRET_KEY` is not set.
  It encrypts the API keys, tokens and passwords saved in pipelines. It is git-ignored: back it up,
  never commit it. For servers prefer setting `DATAFLOW_SECRET_KEY` in `.env` or a secrets manager.

All other settings are environment variables - see `.env.example` in the project root.
