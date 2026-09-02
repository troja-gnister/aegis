# Deployment secrets

Compose reads local development secrets from `deploy/secrets/dev/`. Generate them with
`bash scripts/init-dev-secrets.sh`; the directory is ignored by Git and each file is created
with mode `0600`.

Do not reuse development values in another installation. Production deployments should replace
the Compose secret sources with files provisioned by the deployment environment and readable only
by the account that starts Aegis. Never put secret values in `.env` or commit them to the repository.

`auth-throttle-hmac-key` protects the privacy of login-throttle bucket identifiers. It is mounted
only into the web service; migrations and background workers intentionally start without it.
