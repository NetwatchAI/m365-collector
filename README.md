# m365-collector (`m365c`)

One multi-tenant Python service that collects Microsoft 365 data from Microsoft Graph for
every Netwatch client tenant and exposes it as Prometheus metrics. Prometheus,
Alertmanager and Grafana do storage, alerting and dashboards; Loki keeps the logs.

It replaces the JavaScript Script items in the legacy Zabbix template (`../m365.yaml`).

## Architecture

```
                 ┌──────────────────────────── m365c (one process) ────────────────────────────┐
 tenants.yaml ──►│ ConfigWatcher ──► Scheduler: one asyncio task per (tenant, collector)       │
 thresholds.yaml │                     │  jittered first run, then every interval              │
                 │                     ▼                                                       │
 cert (secret) ─►│ TokenProvider ─► GraphClient (httpx, retries, 429/503 back-off,            │
                 │  1 CertificateCredential   global + per-tenant semaphores, paging, CSV)    │
                 │  per tenant, same app             │                                         │
                 │                                   ▼                                         │
                 │                 collectors: license users mailbox onedrive mailtraffic      │
                 │                             servicehealth securescore appsecrets           │
                 │                                   │ success: replace snapshot               │
                 │                                   ▼ failure: keep snapshot, mark unhealthy  │
                 │                 SnapshotCache[(tenant, collector)]  (+ optional JSON files)  │
                 │                                   │                                         │
                 │   /metrics ◄── Exporter (reads cache + health + thresholds; never calls Graph)
                 │   /healthz /readyz                                                          │
                 └─────────────────────────────────────────────────────────────────────────────┘
                        ▲ scrape 60s
                 Prometheus ──► Alertmanager (route by customer)      Grafana ◄── Loki ◄── Alloy
```

Key properties:

- **One app registration, many tenants.** Tokens are requested per tenant with the same
  client ID and certificate. There are no per-client secrets and no client-secret auth.
- **Collection is separate from exposure.** `/metrics` renders an in-memory snapshot cache.
  A successful run *replaces* the snapshot for that `(tenant, collector)`, so departed
  users disappear instead of lingering. A failed run keeps the previous snapshot and sets
  `m365_collector_success 0`.
- **Labels.** Every series has `customer` and `tenant`. Per-user series carry only `upn`;
  `display_name` and `department` exist only on `m365_user_info`.
- **Pagination everywhere** (`@odata.nextLink`), so large tenants are not cut off.
- **Each tenant report is downloaded once per interval**, not once per user host.
- **Tokens never leave memory**; logs are JSON with a redaction processor; httpx/httpcore
  request logging is suppressed.

### Collectors

| name | default interval | Graph source | metrics (all also `customer`, `tenant`) |
|---|---|---|---|
| `license` | 1h | `GET /subscribedSkus` | `m365_license_total{sku}`, `_consumed`, `_available`, `_suspended`, `_warning` |
| `users` | 6h | `GET /users?$select=…,createdDateTime,signInActivity` (paged) | `m365_user_info{upn,display_name,department,enabled,licensed}`, `m365_user_last_signin_timestamp{upn}` (last *successful* sign-in), `m365_user_created_timestamp{upn}`, `m365_users_total{enabled,licensed}`, `m365_users_disabled_licensed`, `m365_tenant_feature{feature="signin_activity"}` |
| `mailbox` | 12h | `GET /reports/getMailboxUsageDetail(period='D7')` (CSV) | `m365_mailbox_used_bytes{upn}`, `_quota_bytes` (Prohibit Send), `_items`, `_last_activity_timestamp`, `_has_archive`, `m365_reports_concealed{report}` |
| `onedrive` | 6h | `GET /users/{id}/drive?$select=quota` per licensed, enabled user | `m365_onedrive_used_bytes{upn}`, `_total_bytes`, `_deleted_bytes` |
| `mailtraffic` | 12h | `GET /reports/getEmailActivityUserDetail(period='D7')` (CSV) | `m365_mail_sent_7d{upn}`, `_received_7d`, `_read_7d`, `m365_reports_concealed{report}` |
| `servicehealth` | 5m | `GET /admin/serviceAnnouncement/healthOverviews` | `m365_service_status{service,status}` = 1 |
| `securescore` | 24h | `GET /security/secureScores?$top=7`, newest by `createdDateTime` | `m365_securescore_current`, `m365_securescore_max` |
| `appsecrets` | 24h | `GET /applications` (paged) | `m365_app_credential_expiry_timestamp{app,app_id,credential_type,key_id}` |

Built-in metrics: `m365_collector_success{collector}`, `m365_collector_duration_seconds`,
`m365_collector_last_success_timestamp`, `m365_collector_samples`,
`m365_collector_interval_seconds`, `m365_collector_permission_ok{collector}`,
`m365_tenant_auth_ok`, `m365_threshold{check}`, `m365_collector_build_info{version}`,
`m365_graph_requests_total{status}`, `m365_graph_throttled_total`,
`m365_exporter_duplicate_series{collector}` (only when samples were dropped), and
`m365_cert_expiry_timestamp{thumbprint}` (no customer/tenant: it is Netwatch's own cert).

Health semantics:

- `m365_tenant_auth_ok` follows the most recent token request / Graph call of *any*
  collector for the tenant: a definitive token failure (`AADSTS…`: consent removed, cert not
  uploaded) or a Graph 401 sets it to 0; any successful run sets it back to 1, so it recovers
  within one `servicehealth` cycle (5 min). Identity-service outages (no `AADSTS` code,
  network errors) are treated as transient and do not change it.
- `m365_collector_permission_ok{collector}` is 0 while that collector's last run got a Graph
  403 (a missing application permission or licence-gated feature). That is a
  `M365PermissionMissing` warning, not "cannot authenticate".
- A failed run is retried after 5 min, then 10, 20, 40, capped at 1 h (never later than the
  normal interval), instead of waiting a full interval (24 h for `appsecrets`).
- After a restart, persisted snapshots are served again and their collection time is
  exported as `m365_collector_last_success_timestamp`, so `M365CollectorStale` still fires
  if the collector keeps failing; `m365_collector_success` appears after the first real run.

Collector notes:

- **users**: `signInActivity` needs `AuditLog.Read.All` *and* Entra ID P1 in the client
  tenant. If Graph refuses it (403) the collector retries without it, warns once per tenant
  and sets `m365_tenant_feature{feature="signin_activity"} 0`. Only
  `lastSuccessfulSignInDateTime` is used: `lastSignInDateTime` and
  `lastNonInteractiveSignInDateTime` also record *failed* attempts, so an abandoned account
  under password spray would look active. Microsoft only populates the successful timestamp
  for sign-ins since about December 2023, so older activity counts as "never", which is
  inactive either way. Users with no successful sign-in are caught by
  `M365UserInactive{signin="never"}` once their account (`createdDateTime`) is older than
  `inactive_days`.
  Agent identities are excluded with `$filter=not isof('microsoft.graph.agentUser')` (sent as
  an advanced query with `ConsistencyLevel: eventual` and `$count=true`), plus a client-side
  `@odata.type` check. If a tenant answers 400 to the filter, the collector stops sending it
  for that tenant. **Unverified:** `isof` on `/users` is undocumented and `agentUser` is a
  beta type, so whether agent users are really excluded on v1.0 needs a real tenant (see
  "Needs a real tenant").
- **onedrive** fetches its own minimal paged user list
  (`id,userPrincipalName,accountEnabled,assignedLicenses`) instead of depending on the
  `users` run. This keeps intervals, failures and `m365c run --collector onedrive`
  independent, at the cost of one extra paged listing per run. Drive requests run through a
  worker pool no larger than `per_tenant_concurrency`, and the Graph client takes the
  per-tenant slot before the global one, so a big tenant's OneDrive run queues behind
  itself instead of starving other tenants. A 404 (OneDrive not provisioned) skips the user.
  Other per-user errors (403, 5xx after retries) are logged and skipped. The run fails only
  if every drive is refused (permission problem) or more than 10 % of users fail.
  This is one Graph call per licensed user (about 87 % of all calls). If that becomes a
  problem, `GET /reports/getOneDriveUsageAccountDetail(period='D7')` gives the same numbers
  in one CSV per tenant (`Reports.Read.All` is already granted), but daily rather than live.
- **mailbox / mailtraffic** first read `GET /admin/reportSettings`. If
  `displayConcealedNames` is true they emit `m365_reports_concealed{report=…} 1`, skip the
  per-user series, log a warning naming the admin-centre setting, and still succeed. Both the
  flat and the documented `{"value": {…}}` response shapes are accepted; a response without
  the flag fails the run rather than being read as "not concealed". As a second guard,
  rows whose UPN has no `@` (hashed IDs) are never exported, and a report where no row has a
  real UPN is treated as concealed.
  Report rows with `Is Deleted = True` are skipped; empty dates produce no sample.
- **servicehealth** exports `status` in camelCase (the enum's spelling) even if Graph returns
  PascalCase, and the alert regex is case-insensitive as well.

## Entra app registration (once, in the Netwatch tenant)

1. **Entra admin center → App registrations → New registration**
   - Name: `Netwatch M365 Monitoring`
   - Supported account types: **Accounts in any organizational directory (Multitenant)**
   - Redirect URI: *Web* `https://netwatch.example/consent-done` (any page you control; it is
     only where the browser lands after consent).
2. **Certificates & secrets → Certificates → Upload certificate.** Create a key pair, e.g.:

   ```sh
   openssl req -x509 -newkey rsa:3072 -sha256 -days 730 -nodes \
     -subj "/CN=netwatch-m365c" -keyout m365c.key -out m365c.crt
   cat m365c.key m365c.crt > m365c_cert.pem   # what m365c reads: key + cert in one PEM
   ```

   Upload `m365c.crt` only. Do **not** create client secrets; m365c does not support them.
3. **API permissions → Add a permission → Microsoft Graph → Application permissions.** Add
   exactly the output of `m365c permissions`:

   ```
   Application.Read.All
   AuditLog.Read.All
   Files.Read.All
   LicenseAssignment.Read.All
   ReportSettings.Read.All
   Reports.Read.All
   SecurityEvents.Read.All
   ServiceHealth.Read.All
   User.Read.All
   ```

   Grant admin consent in the Netwatch tenant too if you monitor it. No directory role
   (e.g. Global Reader) is needed: application permissions are enough.
4. Put the **Application (client) ID** in `settings.yaml` → `app.client_id`.

## Client onboarding

1. Add the tenant and get the consent link:

   ```sh
   m365c tenant add --tenant-id <tenant-guid> --name contoso.onmicrosoft.com --customer contoso
   ```

   This appends to `tenants.yaml` (hot reloaded) and prints
   `https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<client_id>`.
2. A **Global Administrator** (or Privileged Role Administrator) of the client opens the link
   and accepts. This creates the enterprise app in their tenant with the permissions above.
3. **Turn off report concealment**, otherwise mailbox and mail traffic metrics are skipped:
   Microsoft 365 admin center → **Settings → Org settings → Services → Reports** → clear
   *Display concealed user, group, and site names in all reports*.
4. **Verify:**

   ```sh
   m365c tenant verify contoso
   ```

   ```
   tenant                   check        detail
   contoso.onmicrosoft.com  token        ✅  token acquired
   contoso.onmicrosoft.com  permissions  ✅  9 required roles granted
   contoso.onmicrosoft.com  reports      ✅  report names not concealed
   contoso.onmicrosoft.com  signin       ✅  signInActivity available
   ```

   `signin` ❌ usually means the tenant has no Entra ID P1; the `users` collector still works,
   only `m365_user_last_signin_timestamp` is missing. Permissions are read from the token's
   `roles` claim, decoded locally and never printed.

## Configuration reference

Three files in the config directory (`--config-dir`, `M365C_CONFIG_DIR`, default `./config`;
`/etc/m365c` in the container). Examples are in `config/*.example.yaml`; real files are
git-ignored. All config is validated at startup; errors name the file and the key.

**`settings.yaml`** (read at startup)

| key | default | meaning |
|---|---|---|
| `app.client_id` | required | Application (client) ID of the multi-tenant app |
| `secrets.cert` | required | secret reference of the PEM (key + cert) or PKCS#12 |
| `secrets.cert_password` | none | secret reference of the key/PFX password, if encrypted |
| `http.max_concurrency` | 20 | concurrent Graph requests, all tenants |
| `http.per_tenant_concurrency` | 4 | concurrent Graph requests per tenant |
| `http.timeout_s` | 60 | per request |
| `http.max_retries` | 5 | for 429/500/502/503/504 and network errors |
| `server.host` / `server.port` | `0.0.0.0` / `9365` | HTTP listener |
| `cache.persist_dir` | none | directory for JSON snapshots, so restarts start with data |
| `reload_interval` | `30s` | how often tenants/thresholds files are checked |
| `intervals.<collector>` | collector default | e.g. `mailbox: 12h` (`s`, `m`, `h`, `d`) |

**`tenants.yaml`** (hot reloaded: added tenants start collecting, removed or disabled ones
are dropped from the cache and `/metrics`, including their Graph counters). A change is
applied once the file has been unchanged for one poll (`reload_interval`), so half-written
saves are not picked up. An invalid, unreadable or empty file is logged and ignored, and the
last good configuration stays. The `tenants:` key is required: write `tenants: []` to run
with no tenants. An accidentally emptied file therefore never drops every customer.
`m365c tenant add` validates first, then replaces the file atomically. It rewrites the YAML,
so comments in `tenants.yaml` are not kept.

```yaml
tenants:
  - id: 11111111-1111-1111-1111-111111111111
    name: contoso.onmicrosoft.com   # `tenant` label
    customer: contoso               # `customer` label, lowercase slug
    collectors: all                 # or [license, users, ...]
    enabled: true
```

**`thresholds.yaml`** (hot reloaded). Exported as `m365_threshold{customer,tenant,check}`;
the alert rules join against it, so changing a threshold needs no Prometheus reload.

```yaml
defaults:
  license_util_warn: 0.95
  storage_used_warn: 0.90
  inactive_days: 30
  mail_sent_max_7d: 800
  mail_received_max_7d: 800
customers:
  contoso:
    storage_used_warn: 0.85   # overrides the default for this customer only
```

**Environment overrides.** Every value can be overridden with `M365C_` and `__` for
nesting: `M365C_HTTP__MAX_CONCURRENCY=10`, `M365C_SECRETS__CERT=file:/run/secrets/x`,
`M365C_CACHE__PERSIST_DIR=/var/lib/m365c/cache`, `M365C_TENANTS='[{"id": …}]'` (JSON),
`M365C_THRESHOLDS_DEFAULTS__INACTIVE_DAYS=45`. Environment beats YAML, so a
`M365C_TENANTS` or `M365C_THRESHOLDS_*` variable makes edits to the hot-reloaded file appear
to do nothing; the service logs a warning at startup when one is set. Logging:
`M365C_LOG_LEVEL` (default `INFO`).

## Secret backends

The certificate is loaded once into memory. It is never written to disk or logged. At
startup it is parsed and checked (key present, certificate matches the key, password right),
so a broken certificate stops `m365c serve` with a clear error instead of failing every token
request later. Its expiry is logged (warning within 30 days, error if expired) and exported
as `m365_cert_expiry_timestamp`, which drives `M365CertificateExpiring`.

| reference | backend | needs |
|---|---|---|
| `file:/run/secrets/m365c_cert` | file | nothing (default, used by Compose secrets) |
| `vault:secret/m365c/cert#pem` | HashiCorp Vault KV v2: mount `secret`, path `m365c/cert`, key `pem` | `pip install m365c[vault]`; `VAULT_ADDR`; AppRole (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`) or Kubernetes (`VAULT_K8S_ROLE`, optional `VAULT_K8S_TOKEN_PATH`, `VAULT_K8S_MOUNT`) or `VAULT_TOKEN`; optional `VAULT_NAMESPACE` |
| `keyvault:https://<kv>.vault.azure.net/<name>` | Azure Key Vault secret (a Key Vault *certificate* is readable as a secret of the same name; PKCS#12 is base64-decoded automatically) | `pip install m365c[azure]`; `DefaultAzureCredential` (managed identity, workload identity, `AZURE_*` env) |

The container image includes both extras.

## Certificate rotation

No downtime, no client involvement (consent is per app, not per certificate):

1. Create a new key pair (see above) and **upload the new certificate** to the app
   registration. Keep the old one; both are now valid.
2. **Switch the secret**: replace `deploy/secrets/m365c_cert.pem` (or the Vault/Key Vault
   value) and restart the collector: `docker compose -f deploy/docker-compose.yml up -d
   --force-recreate collector`.
3. **Verify**: the startup log shows `certificate_loaded` with the new thumbprint, and
   `m365_cert_expiry_timestamp{thumbprint=…}` shows the new one. Run
   `m365c tenant verify <customer>` for a few tenants, and check in Prometheus that
   `min(m365_tenant_auth_ok) == 1` within about 5 minutes (the `M365TenantAuthFailed` alert
   stays quiet).
4. **Remove the old certificate** from the app registration.

## Deploy

```sh
cp config/settings.example.yaml config/settings.yaml      # set app.client_id
cp config/thresholds.example.yaml config/thresholds.yaml
m365c -c config tenant add ...                             # or copy tenants.example.yaml
# secrets (git-ignored), see deploy/secrets/README.md
cp m365c_cert.pem deploy/secrets/m365c_cert.pem
openssl rand -base64 24 > deploy/secrets/grafana_admin_password
printf '%s' 'smtp-password' > deploy/secrets/alertmanager_smtp_password
printf '%s' 'https://hooks.example/...' > deploy/secrets/alertmanager_webhook_contoso
docker compose -f deploy/docker-compose.yml up -d --build
```

- Collector: `:9365/metrics`, `/healthz` (process up), `/readyz` (config loaded and at least
  one collector run attempted, or no tenants configured). Not published on the host by
  default. `/metrics` is rendered off the event loop, so a large scrape doesn't stall Graph
  calls or the healthcheck. The image healthcheck follows `M365C_SERVER__PORT`.
- Prometheus `127.0.0.1:9090` (scrapes every 60s, rules in `deploy/prometheus/rules/m365.yml`),
  Alertmanager `127.0.0.1:9093`, Grafana `127.0.0.1:3000` (dashboard *Microsoft 365 overview*,
  `customer` and `tenant` variables). Put a reverse proxy with TLS in front for remote access.
- Alloy ships container logs labelled `logs.netwatch/ship=true` to Loki, with `level`,
  `customer` and `collector` extracted as labels. Alloy does not mount the Docker socket
  (root-equivalent on the host). It talks to a `docker-socket-proxy` that only allows
  `GET /containers/*` and `GET /networks`; any POST is refused (checked: 403).

**Personal data.** Logs and the snapshot cache contain user principal names, and the cache
also holds display names and departments (`m365_user_info`). With `cache.persist_dir` set
(on by default in Compose: the `m365c-cache` volume at `/var/lib/m365c/cache`), they are
stored as JSON files, one per tenant and collector, created with mode 0600. A file is
replaced on every successful run and deleted when its tenant or collector is removed. Files
of tenants removed while the service was down are deleted at the next start. Loki keeps logs
for 30 days (`retention_period: 720h`). Treat both volumes as customer data:
`docker compose down -v` removes them.
- Alertmanager groups by `customer, alertname` and has an example per-customer route
  (email + webhook). All credentials are `*_file` references to Compose secrets.

### Alerts

| Alert | Fires when | Severity |
|---|---|---|
| `M365LicenseNearlyExhausted` | consumed/total > `license_util_warn` | warning |
| `M365LicenseExhausted` | available ≤ 0 and total > 0 (normal for many small clients, so warning) | warning |
| `M365MailboxNearlyFull` / `M365OneDriveNearlyFull` | used/quota > `storage_used_warn` | warning |
| `M365DisabledUserLicensed` | disabled users hold licences, for 1d | warning |
| `M365UserInactive` | licensed, enabled user: last successful sign-in older than `inactive_days` (`signin="stale"`), or never signed in and account older than `inactive_days` (`signin="never"`); only where signInActivity is available | info |
| `M365HighMailTraffic` | sent or received over threshold (`direction` label) | warning |
| `M365ServiceDegraded` | a service is degraded/interrupted/investigating (case-insensitive) | warning |
| `M365AppCredentialExpiring` | a secret/certificate expires within 30 days. Already-expired credentials don't alert (they'd fire for ever); they are listed on the dashboard | warning |
| `M365TenantAuthFailed` | token failure or Graph 401 | critical |
| `M365PermissionMissing` | a collector got Graph 403 | warning |
| `M365ReportsConcealed` | report names concealed | warning |
| `M365CollectorFailing` | last run failed, for 3h | warning |
| `M365CollectorStale` | no success for 2× the collector's interval | warning |
| `M365CollectorDown` | Prometheus cannot scrape the collector (`customer="netwatch"`) | critical |
| `M365CertificateExpiring` | the app certificate expires within 30 days (`customer="netwatch"`) | critical |

Each has `customer`, `tenant` and a `summary` naming the user, SKU, app or service.
Alertmanager inhibits `CollectorFailing`/`CollectorStale`/`PermissionMissing` while
`TenantAuthFailed` fires for the same tenant.

**Validation runs in the test suite.** `tests/test_deploy.py` runs `promtool check rules`,
`promtool check config`, `promtool test rules` (a unit test per alert, firing and not firing,
in `deploy/prometheus/tests/m365_test.yml`), `amtool check-config`, and parses every
dashboard query with promtool. It uses a local promtool/amtool if one is installed,
otherwise the official images through Docker. Without either it skips locally, but **fails
when `CI` is set**, so CI needs Docker or promtool. By hand:

```sh
docker run --rm -v "$PWD/deploy/prometheus:/p" -w /p --entrypoint promtool prom/prometheus:v3.5.0 check rules rules/m365.yml
docker run --rm -v "$PWD/deploy/prometheus:/p" -w /p/tests --entrypoint promtool prom/prometheus:v3.5.0 test rules m365_test.yml
docker run --rm -v "$PWD/deploy/alertmanager:/a" --entrypoint amtool prom/alertmanager:v0.28.1 check-config /a/alertmanager.yml
```

The dashboard JSON is generated: edit `deploy/grafana/gen_dashboard.py`, then run
`python deploy/grafana/gen_dashboard.py deploy/grafana/dashboards/m365-overview.json`.

## CLI

```
m365c serve                                   # the service (container default)
m365c permissions                             # Graph application permissions to grant
m365c tenant add --tenant-id <guid> --name <domain> --customer <slug>
m365c tenant list
m365c tenant verify <customer|tenant-name|tenant-id>
m365c run --tenant <x> [--collector <name> ...] [--dry-run] [--json]
```

Global options: `-c/--config-dir`, `--log-level`. `run --dry-run` writes neither the cache
nor the persistence directory. `serve`, `run` and `tenant verify` accept a hidden
`--fixtures <dir>` that serves Graph from fixture files with a fake token, for offline demos
and for testing the deploy stack end to end without a tenant:

```sh
uv run m365c -c tests/fixtures/config run --tenant contoso --dry-run --fixtures tests/fixtures/graph
```

## Local development

Requires [uv](https://docs.astral.sh/uv/). Works on Windows (PowerShell, Git Bash) and Linux.

```sh
uv sync --all-extras          # Python 3.12 venv with dev tools
uv run pytest                 # offline: Graph and token traffic are mocked
uv run ruff check
uv run ruff format --check
uv run mypy src               # strict
docker compose -f deploy/docker-compose.yml config --quiet
```

Layout: `src/m365c/` (package), `tests/` (fixtures in `tests/fixtures/graph`, fake data
only), `config/` (examples), `deploy/` (Compose stack, Prometheus rules, Alertmanager,
Grafana, Loki, Alloy).

Adding a collector: subclass `BaseCollector` in `src/m365c/collectors/` with `name`,
`default_interval`, `required_permissions`, `metric_help` and `async collect(ctx)`, register it
in `collectors/__init__.py`, add fixtures and a test, then update the permission list above.

## Deviations from the handover spec

| Spec | Implementation | Why |
|---|---|---|
| Retry 429 and 503/504 | Also retries 500, 502 and network errors (same backoff) | Transient Graph errors; one blip shouldn't fail a daily run |
| `m365_reports_concealed` with `customer`, `tenant` | Extra `report` label (`mailbox` / `mailtraffic`) | Both collectors emit it; identical label sets would make Prometheus reject the whole scrape |
| `m365_tenant_auth_ok` 0 on any `GraphAuthError` | 0 on definitive token failures and Graph 401; a 403 sets `m365_collector_permission_ok{collector}` 0 instead | A single missing permission is not "cannot authenticate", and the old rule kept the alert firing for up to 24 h after a fix (review M4) |
| Last sign-in timestamp | `lastSuccessfulSignInDateTime` only, plus `m365_user_created_timestamp` | The other fields include failed attempts; never-signed-in users need a signal (review H4) |
| `securescore` `$top=1` | `$top=7`, newest by `createdDateTime` | The order isn't documented |
| `M365LicenseExhausted` `available == 0`, critical | `available <= 0` (over-assignment), severity warning | Fully assigned SKUs are normal for small clients |
| `M365AppCredentialExpiring` `< 30d` | `0 < remaining < 30d` | Expired, never-removed secrets would alert for ever |
| `M365CollectorStale` "2× interval, or 2d" | Uses exported `m365_collector_interval_seconds` | Exact per collector |
| Built-in metrics | Extra `m365_collector_interval_seconds`, `m365_collector_permission_ok`, `m365_exporter_duplicate_series`, `m365_cert_expiry_timestamp` | See health semantics above |
| Alerts | Extra `M365CollectorDown`, `M365PermissionMissing`, `M365CertificateExpiring` | Cover the exporter itself, permissions and our own certificate |
| Settings | Extra `secrets.cert_password`, `reload_interval` | Encrypted keys / PFX; configurable reload |
| Failed runs wait a full interval | Retry backoff 5 min → 1 h | One transient failure no longer means a 24 h gap |
| Log shipping via the Docker socket | Via a read-only docker-socket-proxy | The socket is root-equivalent |
| Build backend unspecified | `uv_build` | hatchling's build env deadlocked uv's cache lock on the dev machine |
| — | Hidden `--fixtures` on `serve`, `run`, `tenant verify` | Offline demos and end-to-end stack tests |

## Needs a real tenant

Everything above is verified offline (unit tests, promtool/amtool, an end-to-end Compose run
with fixture data). These need one consented test tenant before the first client:

1. `m365c tenant verify`: token, the 9 roles in the token, concealment, signInActivity.
   Revoke consent and confirm `m365_tenant_auth_ok` drops to 0 within about 5 min (expect
   `AADSTS…` in the error), then re-grant and confirm recovery.
2. Certificate rotation with two certificates; an `AADSTS700027`-style error for the
   removed one.
3. signInActivity without P1: the exact 403 code, the fallback fires once, and
   `m365_tenant_feature` = 0. Repeat with P1 but without `AuditLog.Read.All`.
4. The agent-user filter: is the `isof` filter accepted on v1.0 (200 or 400)? Do agent users
   carry `@odata.type`? Are they returned at all?
5. Page size with signInActivity and throttling on large tenants.
6. The report 302 flow, the download host, the BOM, and the exact CSV headers.
7. The `/admin/reportSettings` response shape (flat or `value`-wrapped); toggle concealment.
8. healthOverviews `status` casing.
9. OneDrive app-only `GET /users/{id}/drive?$select=quota` with `Files.Read.All`: the
   statuses for never-provisioned and Exchange-only users, and a full-run timing on the
   largest tenant.
10. secureScores order and date.
11. appsecrets `endDateTime` population; include the Netwatch tenant so this app's own
    certificate is monitored too.
12. All collectors at once: `m365_graph_throttled_total` and `Retry-After` values.
