# m365-collector (`m365c`)

One multi-tenant Python service that collects Microsoft 365 data from Microsoft Graph for
every Netwatch client tenant and exposes it as Prometheus metrics. Prometheus,
Alertmanager and Grafana do storage, alerting and dashboards; Loki keeps the logs.

It replaces the JavaScript Script items in the legacy Zabbix templates (`../m365.yaml` and
the per-client variant `../liberty_m365.yaml`) and is a drop-in replacement for them: it
authenticates with each client's existing app registration and **client secret**, and needs
only the Graph permissions those apps already have (see [Template parity](#template-parity)).
The certificate-based single multi-tenant app is supported as well, per tenant.

## Architecture

```
                 ┌──────────────────────────── m365c (one process) ────────────────────────────┐
 tenants.yaml ──►│ ConfigWatcher ──► Scheduler: one asyncio task per (tenant, collector)       │
 thresholds.yaml │                     │  jittered first run, then every interval              │
                 │                     │  gate: token roles ⊇ collector's required groups?     │
 secret refs ───►│ TenantTokenProvider ─► GraphClient (httpx, retries, 429/503 back-off,      │
 (file/env/      │  per tenant: certificate │  global + per-tenant semaphores, paging, CSV)   │
  vault/kv)      │  or client_secret, lazy  │                                                  │
                 │  secrets, rotation       ▼                                                  │
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

- **Per-tenant auth.** Each tenant uses either its own app registration with a client
  secret (what the Zabbix templates used) or the shared Netwatch multi-tenant app with a
  certificate. `settings.yaml` `app:` is the default; a tenant's `auth:` block overrides it.
  See [Authentication](#authentication).
- **Permission-aware.** After the first token for a tenant, m365c reads the token's `roles`
  claim and runs only the collectors whose permissions are granted; the rest are skipped
  with a metric and one warning. See [Permission gating](#permission-gating).
- **Collection is separate from exposure.** `/metrics` renders an in-memory snapshot cache.
  A successful run *replaces* the snapshot for that `(tenant, collector)`, so departed
  users disappear instead of lingering. A failed run keeps the previous snapshot and sets
  `m365_collector_success 0`.
- **Labels.** Every series has `customer` and `tenant`. Per-user series carry `upn`; display
  names and departments are on the `*_info` metrics only.
- **Pagination everywhere** (`@odata.nextLink`), so large tenants are not cut off (the
  templates read a single page).
- **Each tenant report is downloaded once per interval**, not once per user host.
- **Secrets and tokens never leave memory**; logs are JSON with a redaction processor that
  also scrubs the literal value of every loaded secret; httpx/httpcore request logging is
  suppressed.

### Collectors

| name | default interval | Graph source | required permission (any one per line) | metrics (all also `customer`, `tenant`) |
|---|---|---|---|---|
| `license` | 1h | `GET /subscribedSkus` | `Organization.Read.All` or `LicenseAssignment.Read.All` | `m365_license_total{sku,sku_name}`, `_consumed`, `_available`, `_suspended`, `_warning` |
| `users` | 6h | `GET /users?$select=…,assignedLicenses,createdDateTime[,signInActivity]` (paged) + one `GET /subscribedSkus` | `User.Read.All` (optional: `AuditLog.Read.All` for sign-in; `Organization.Read.All` or `LicenseAssignment.Read.All` for SKU names) | `m365_user_info{upn,display_name,department,enabled,licensed}`, `m365_user_license{upn,sku,sku_id,sku_name}`, `m365_user_last_signin_timestamp{upn}` (last *successful* sign-in), `m365_user_last_activity_timestamp{upn}`, `m365_user_created_timestamp{upn}`, `m365_users_total{enabled,licensed}`, `m365_users_disabled_licensed`, `m365_tenant_feature{feature="signin_activity"}` |
| `mailbox` | 12h | `GET /reports/getMailboxUsageDetail(period=…)` (CSV) | `Reports.Read.All` (optional: `ReportSettings.Read.All`) | `m365_mailbox_info{upn,display_name,has_archive}`, `m365_mailbox_used_bytes{upn}`, `_quota_bytes` (Prohibit Send), `_items`, `_last_activity_timestamp`, `_has_archive`, `m365_mailbox_deleted`, `m365_reports_concealed{report,source}` |
| `onedrive` | 6h | `source: report` (default): `GET /reports/getOneDriveUsageAccountDetail(period=…)` (CSV); `source: quota`: `GET /users/{id}/drive?$select=quota` per licensed, enabled user | report: `Reports.Read.All`; quota: `User.Read.All` and `Files.Read.All` | `m365_onedrive_info{upn,display_name}` (report), `m365_onedrive_used_bytes{upn}`, `_total_bytes`, `_file_count` (report), `_last_activity_timestamp` (report), `_deleted_bytes` (quota), `m365_reports_concealed{report,source}` |
| `mailtraffic` | 12h | `GET /reports/getEmailActivityUserDetail(period=…)` (CSV) | `Reports.Read.All` (optional: `ReportSettings.Read.All`) | `m365_mail_sent{upn,period}`, `m365_mail_received{upn,period}`, `m365_mail_read{upn,period}`, `m365_reports_concealed{report,source}` |
| `servicehealth` | 5m | `GET /admin/serviceAnnouncement/healthOverviews` | `ServiceHealth.Read.All` | `m365_service_status{service,status}` = 1 |
| `securescore` | 24h | `GET /security/secureScores?$top=7`, newest by `createdDateTime` | `SecurityEvents.Read.All` | `m365_securescore_current`, `m365_securescore_max` |
| `appsecrets` | 24h | `GET /applications` (paged) | `Application.Read.All` | `m365_app_credential_expiry_timestamp{app,app_id,credential_type,key_id}` |

`m365c permissions` prints this table from the code.

Built-in metrics: `m365_collector_success{collector}`, `m365_collector_enabled{collector}`
(1 runs, 0 skipped), `m365_collector_skipped{collector,reason,permission}`,
`m365_collector_duration_seconds`, `m365_collector_last_success_timestamp`,
`m365_collector_samples`, `m365_collector_interval_seconds`,
`m365_collector_permission_ok{collector}`, `m365_tenant_auth_ok`,
`m365_tenant_auth_error{reason}`, `m365_tenant_permission{permission}`,
`m365_collector_expected{collector}`, `m365_report_rows_skipped{report,reason}`,
`m365_client_secret_expiry_timestamp{client_id}`,
`m365_tenant_cert_expiry_timestamp{thumbprint}` (a tenant's own `auth.cert`),
`m365_threshold{check}`,
`m365_collector_build_info{version}`, `m365_graph_requests_total{status}`,
`m365_graph_throttled_total`, `m365_exporter_duplicate_series{collector}` (only when samples
were dropped), and `m365_cert_expiry_timestamp{thumbprint}` (no customer/tenant: the shared
default certificate).

Health semantics:

- `m365_tenant_auth_ok` is 0 and `m365_tenant_auth_error{reason}` says why while either
  - the tenant has a definitive token failure (an `AADSTS…` code, a secret that cannot be
    loaded, a token with no application roles: `no_roles`, or identity errors lasting 30
    min: `identity_unreachable`). Token failures are tenant-wide: the next run of *any*
    collector that gets a token clears it, so it recovers within one `servicehealth` cycle;
  - or *any* collector's last run got a Graph 401. That is tracked per collector and only
    cleared by that collector's next run, so another collector's success cannot hide it.

  Short identity-service outages (no `AADSTS` code, network errors) are transient and do
  not change it.
- `m365_collector_permission_ok{collector}` is 0 while that collector's last run got a Graph
  403 (a permission that is in the token but refused, or a licence-gated feature). That is a
  `M365PermissionMissing` warning, not "cannot authenticate".
- A **skipped** collector (permission not in the token) exports `m365_collector_enabled 0`
  and `m365_collector_skipped`, but no success, duration or last-success series, so it
  raises no `CollectorFailing`/`CollectorStale` alerts. Its previous snapshot is dropped.
  A token outage leaves it skipped (no failed run for a collector that is not meant to run).
  If the tenant is *expected* to run it (`m365_collector_expected 1`: listed explicitly in
  `collectors:`, or a template-parity collector under `collectors: all`),
  `M365CollectorSkipped` fires; `M365PermissionRevoked` fires when a role that was in the
  token disappears.
- A failed run is retried after 5 min, then 10, 20, 40, capped at 1 h (never later than the
  normal interval). A skipped collector re-checks the token at least hourly.
- After a restart, persisted snapshots are served again and their collection time is
  exported as `m365_collector_last_success_timestamp`, so `M365CollectorStale` still fires
  if the collector keeps failing; `m365_collector_success` appears after the first real run.

Collector notes:

- **license**: `sku` is the `skuPartNumber`; `sku_name` is the product display name from the
  same bundled map as `m365_user_license` (see *users*), falling back to `sku`.
- **users**: `m365_user_license` comes from each user's `assignedLicenses`; `skuId` is mapped
  to `skuPartNumber` with **one** `subscribedSkus` call per run (never `licenseDetails` per
  user). Without `Organization.Read.All`/`LicenseAssignment.Read.All`, or if that call fails,
  `sku` falls back to the SKU ID. `sku_name` is the product display name (e.g. *Microsoft 365
  Business Premium*) from a bundled copy of Microsoft's licensing CSV, keyed by SKU ID, so it
  needs no extra permission; SKUs missing from it fall back to `sku`. Refresh the copy with
  `scripts/update_sku_names.py`. `signInActivity` needs `AuditLog.Read.All` *and* Entra ID
  P1. If the token lacks `AuditLog.Read.All` it is not requested at all; if Graph refuses it
  (403, no P1) the collector retries without it and warns once. Either way
  `m365_tenant_feature{feature="signin_activity"}` is 0. Only
  `lastSuccessfulSignInDateTime` is used: `lastSignInDateTime` (the templates' field) and
  `lastNonInteractiveSignInDateTime` also record *failed* attempts, so an abandoned account
  under password spray would look active. Microsoft only populates the successful timestamp
  for sign-ins since about December 2023.
  `m365_user_last_activity_timestamp` is the last successful sign-in, or else the mailbox
  report's *Last Activity Date* taken from the `mailbox` collector's latest snapshot for the
  tenant (so it appears from the first `users` run after a `mailbox` run). The dashboard
  does not depend on that coupling; it uses the same fallback as a query:
  `m365_user_last_signin_timestamp or on(customer, tenant, upn) m365_mailbox_last_activity_timestamp`.
  Agent identities are excluded with `$filter=not isof('microsoft.graph.agentUser')` (sent as
  an advanced query) plus a client-side `@odata.type` check; a tenant that answers 400 to the
  filter stops getting it. **Unverified** on v1.0 (see "Needs a real tenant").
- **onedrive**: `source` per tenant (`onedrive.source`, default from `settings.yaml`):
  `report` (default) reads one CSV per tenant with `Reports.Read.All` only, as the template
  did: used, allocated (exported as `_total_bytes`), file count, last activity, display
  name; deleted OneDrives are skipped. `quota` is the live per-user `/drive` method
  (`User.Read.All` + `Files.Read.All`; adds recycle-bin bytes, one call per licensed user,
  worker pool bounded by `per_tenant_concurrency`; a 404 skips the user; 403s on single
  drives are logged but not counted as failures; the run fails only if every drive is
  refused or more than 10 % fail with other errors). `auto` uses `quota` when the token holds
  both roles, otherwise `report`. Gating applies to the effective source.
- **mailbox / mailtraffic / onedrive (report)** use the tenant's report period
  (`reports.period`: `D7`, `D30`, `D90`, `D180`; the template macro `{$MS.GRAPH.DAYS.BACK}`).
  Concealed names: if the token holds `ReportSettings.Read.All` they read
  `GET /admin/reportSettings` (`displayConcealedNames`; flat and `{"value": {…}}` shapes;
  a response without the flag fails the run rather than being read as "not concealed").
  Without that permission, or on a 403, concealment is inferred from the report: no UPN
  contains `@` and at least half look like 32-character hex hashes. Either way
  `m365_reports_concealed{report, source="setting"|"heuristic"}` is exported, per-user series
  are skipped when concealed, and the run still succeeds. Rows whose UPN has no `@` are
  never exported; they are counted in `m365_report_rows_skipped{report,reason="no_upn"}` and
  logged once, so a partially hashed report does not make users vanish silently. Rows with `Is Deleted = True` are skipped (counted in
  `m365_mailbox_deleted`); empty dates produce no sample.
- **mailtraffic** keeps users with zero or empty counts (exported as 0), as the Liberty
  template's fix did; CSV parsing is RFC 4180 (quoted display names with commas are fine).
- **servicehealth** exports `status` in camelCase even if Graph returns PascalCase.

## Authentication

Two methods, chosen per tenant:

| method | credential | what it needs |
|---|---|---|
| `client_secret` | azure-identity `ClientSecretCredential` | the client's app registration: client ID + a client secret reference |
| `certificate` | azure-identity `CertificateCredential` | an app registration with an uploaded certificate (the Netwatch multi-tenant app, or a tenant's own) |

New methods (workload identity, federated credentials) plug in as another
`AuthMethodHandler` in `src/m365c/auth.py` (`AUTH_METHODS`) plus a `method` value in
`config.py`; nothing else changes.

```yaml
# settings.yaml: the default for tenants without their own auth block
app:
  method: certificate            # certificate (default) | client_secret
  client_id: 00000000-0000-0000-0000-000000000000
  # client_secret: file:/run/secrets/default_client_secret   # if method: client_secret
secrets:
  cert: file:/run/secrets/m365c_cert   # only needed if some tenant uses the default certificate

# tenants.yaml
tenants:
  - id: 11111111-1111-1111-1111-111111111111
    name: contoso.onmicrosoft.com
    customer: contoso
    auth:
      method: client_secret
      client_id: 33333333-3333-3333-3333-333333333333
      client_secret: file:/run/m365c-secrets/contoso_client_secret   # a reference, never the value
      client_secret_expires: 2027-03-01                              # optional, see below
    collectors: all
  - id: 22222222-2222-2222-2222-222222222222
    name: fabrikam.onmicrosoft.com
    customer: fabrikam
    auth:
      method: certificate
      cert: vault:secret/fabrikam/m365c#pem      # optional; default: secrets.cert
      # client_id defaults to app.client_id
```

- **Backwards compatible.** Files without `method` or tenant `auth` behave exactly as before
  (certificate, `app.client_id`, `secrets.cert`). `secrets.cert` is optional when no tenant
  uses it. When a tenant does (or there are no tenants), it is checked at startup and a
  broken one stops the service, as before; otherwise a broken default certificate is only
  logged.
- **References only.** Secrets are given as `file:`, `env:`, `vault:` or `keyvault:`
  references (see [Secret backends](#secret-backends)). Anything that does not parse as one is
  treated as a pasted secret and rejected with a validation error that **does not echo it**.
- **Lazy, per tenant.** A tenant's secret is loaded on its first token request. If it cannot
  be loaded (missing file, unset variable, Vault error), only that tenant fails:
  `m365_tenant_auth_ok 0`, `m365_tenant_auth_error{reason="secret_unavailable"}`, one log line
  naming the backend and the problem (never the value). It is retried on the next cycle.
- **Rotation.** Credentials are cached under `(tenant, method, client_id, secret
  fingerprint)`. A `file:` secret's mtime/size is re-checked at most once a minute: replace
  the file and the next token request builds a new credential (the old one is closed). A
  changed `auth:` block (hot reload of `tenants.yaml`) re-resolves the secret. A definitive
  auth failure also re-reads the secret on the next attempt, so a fixed Vault/Key Vault value
  is picked up without a restart.
- **Errors.** Identity-service errors are reduced to a known reason and a fixed
  description; the raw `error_description` (which may echo request data) is never logged or
  re-raised, and azure-identity's own failure warnings are silenced.

  | Entra error | `reason` | meaning |
  |---|---|---|
  | `AADSTS7000215` | `invalid_client_secret` | invalid client secret |
  | `AADSTS7000222` | `secret_expired` | client secret expired (also raises `M365TenantAuthFailed` with `reason="secret_expired"`) |
  | `AADSTS700016` | `app_not_found` | app not found in the tenant (wrong client ID, or no consent) |
  | `AADSTS90002` | `tenant_not_found` | tenant not found |
  | `AADSTS65001` | `consent_missing` | admin consent missing |
  | `AADSTS700027` | `invalid_certificate` | certificate not accepted (not uploaded?) |
  | `AADSTS7000112` | `app_disabled` | application disabled |
  | other `AADSTS…` | `auth_failed` | |
  | secret cannot be loaded | `secret_unavailable` | the log names the file/backend; for files, the uid that must be able to read it |
  | token without application roles | `no_roles` | Entra omits the `roles` claim when nothing is granted: consent revoked |
  | identity service failing without an AADSTS code for 30 min | `identity_unreachable` | e.g. an intercepting proxy, or the endpoint unreachable |
  | Graph 401 | `unauthorized` | |

- **Client secret expiry.** An app cannot read its own secret's expiry without
  `Application.Read.All`, which the client apps don't have. Put the date from the app
  registration (*Certificates & secrets → Expires*) in `auth.client_secret_expires`; it is
  exported as `m365_client_secret_expiry_timestamp` and `M365ClientSecretExpiring` fires 30
  days before (and after, until renewed).

## Permission gating

Each collector declares `required_permissions` as a list of *any-of* groups (every group
must be satisfied by one of its roles) and `optional_permissions` that add data. After the
first successful token for a tenant, m365c decodes the token's `roles` claim locally (the
token is never logged or printed) and, before every run:

- runs the collector if every required group is satisfied;
- otherwise skips it: one warning per tenant and collector (`collector_skipped`, naming the
  missing permission), `m365_collector_enabled 0`,
  `m365_collector_skipped{reason="missing_permission",permission="A|B"} 1`, and the old
  snapshot is dropped. Re-checked every run (at least hourly), so newly granted consent is
  picked up with the next token, without a restart.

`collectors: all` means every collector the tenant's permissions allow. An explicit list
that names a collector without its permissions logs the warning, skips it and reports it;
it never crashes. `m365_tenant_permission{permission}` shows, per tenant, which of the roles
m365c uses are in the token (1) or not (0).

A JWT **without** a `roles` claim (or with an empty one) means the app has no application
permission in the tenant (Entra omits the claim then: consent revoked or never granted).
That is an auth failure, `M365TenantAuthFailed` with `reason="no_roles"`, not a silent skip.
Only a token that is not a JWT at all disables gating (everything runs).

Loss of monitoring is never silent:

| situation | alert |
|---|---|
| consent revoked, no roles left | `M365TenantAuthFailed{reason="no_roles"}` (critical) |
| a role that was granted is removed | `M365PermissionRevoked` (role was in the token within 7 days) |
| an expected collector cannot run | `M365CollectorSkipped` (explicit `collectors:` entry, or parity collector under `all`; after 1 h) |
| a role is in the token but Graph refuses (403) | `M365PermissionMissing` |

```sh
m365c permissions             # required vs optional per collector + minimal parity set
m365c permissions --parity    # just the four parity permissions
m365c permissions --all       # flat list of every role any collector can use
m365c tenant verify contoso   # per collector: runs / skipped (missing X) / what optional adds
```

## Template parity

The full matrix (every Script item, dependent item, discovery rule, LLD macro and widget of
both template files → metric → status) is in [`docs/parity.md`](docs/parity.md). Summary:

| Template | Covered by | Status |
|---|---|---|
| M365 license audit (SKU discovery, total/used/available) | `license` | ✅ |
| M365 disabled users (discovery + department, licences, last activity) | `users`: `m365_user_info`, `m365_user_license`, `m365_user_last_activity_timestamp` | ✅ / ⚠️ successful sign-ins only, v1.0 |
| M365 mailbox discovery / size audit (used, last activity, archive, F1/G3 lists) | `mailbox` + `m365_user_license` (F1/G3 as PromQL) | ✅ / ⚠️ size filter and F1/G3 split are queries |
| M365 mailbox inactivity audit (D30, `isDeleted`) | `mailbox` with `reports.period`, `m365_mailbox_deleted`, `inactive_days` | ✅ / ⚠️ inactivity is a query |
| M365 onedrive discovery / size audit (used, allocated, %, free, files, last activity) | `onedrive` `source: report` | ✅ / ⚠️ size filter is a query |
| M365 send receive audit (+ discovery above `{$MAILS.*}`) | `mailtraffic`, `M365HighMailTraffic` | ✅ / ⚠️ threshold filter is a query |
| Four dashboard widgets | Grafana row "Template parity" | ✅ |

Everything is collected with **only** the permissions the legacy apps have. The minimal set
(`m365c permissions --parity`):

```
Reports.Read.All
User.Read.All
AuditLog.Read.All                                     (optional: sign-in activity)
Organization.Read.All or LicenseAssignment.Read.All
```

`servicehealth`, `securescore` and `appsecrets` are outside the parity set: they are skipped
for the legacy apps (and run when the permissions are granted). Size and traffic macros
became thresholds (`mailbox_size_gb_min`, `onedrive_size_gb_min`, `mail_sent_max`,
`mail_received_max`, `inactive_days`) exported as `m365_threshold{check}`; dashboards and
alerts filter on them, nothing is filtered at collection time.

## Entra app registration (Netwatch multi-tenant app, certificate method)

Only needed for tenants that use the shared certificate app. Tenants migrated from Zabbix
keep their own app registration and client secret (see the next section but one).

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

   Upload `m365c.crt` only. The key must be RSA (azure-identity signs with RS256).
3. **API permissions → Add a permission → Microsoft Graph → Application permissions.** For
   every collector, grant `m365c permissions --all`:

   ```
   Application.Read.All
   AuditLog.Read.All
   Files.Read.All
   LicenseAssignment.Read.All
   Organization.Read.All          (alternative to LicenseAssignment.Read.All; one is enough)
   ReportSettings.Read.All
   Reports.Read.All
   SecurityEvents.Read.All
   ServiceHealth.Read.All
   User.Read.All
   ```

   Anything less is fine: collectors without their permissions are skipped. No directory role
   is needed. Broader roles that Graph also accepts for the same calls count too, so apps
   that already hold them (often the legacy Zabbix apps) need no new consent:
   `Directory.Read.All`, `Directory.ReadWrite.All`, `Organization.ReadWrite.All` and
   `LicenseAssignment.ReadWrite.All` for licences; `Directory.Read.All`,
   `Directory.ReadWrite.All` and `User.ReadWrite.All` for users. `m365c permissions` lists
   only the least-privileged roles.
4. Put the **Application (client) ID** in `settings.yaml` → `app.client_id`.

## Client onboarding

1. Add the tenant:

   ```sh
   # shared certificate app (default auth from settings.yaml)
   m365c tenant add --tenant-id <tenant-guid> --name contoso.onmicrosoft.com --customer contoso
   # the client's own app registration with a client secret
   m365c tenant add --tenant-id <tenant-guid> --name contoso.onmicrosoft.com --customer contoso \
     --auth client-secret --client-id <app-guid> \
     --client-secret-ref file:/run/m365c-secrets/contoso_client_secret \
     --client-secret-expires 2027-03-01
   # a tenant-specific certificate
   m365c tenant add ... --auth certificate [--client-id <app-guid>] --cert-ref vault:secret/contoso/m365c#pem
   ```

   `tenant add` writes the tenant's `auth:` block and **never accepts a literal secret**: the
   secret options take references only, and a value that is not a reference is refused
   without being echoed. It appends to `tenants.yaml` (hot reloaded) and prints the
   admin-consent URL for the effective client ID.
2. For the shared app, a **Global Administrator** (or Privileged Role Administrator) of the
   client opens the consent link and accepts. Apps used by the Zabbix templates are already
   consented.
3. **Turn off report concealment** (or grant `ReportSettings.Read.All` so m365c can say
   why per-user report series are missing): Microsoft 365 admin center → **Settings → Org
   settings → Services → Reports** → clear *Display concealed user, group, and site names in
   all reports*.
4. **Verify:**

   ```sh
   m365c tenant verify contoso
   ```

   ```
   tenant                   check                  detail
   contoso.onmicrosoft.com  auth               ✅  client_secret, client 33333333-…
   contoso.onmicrosoft.com  token              ✅  token acquired
   contoso.onmicrosoft.com  permissions        ✅  granted: AuditLog.Read.All, Organization.Read.All, Reports.Read.All, User.Read.All
   contoso.onmicrosoft.com  collector license  ✅  runs
   contoso.onmicrosoft.com  collector users    ✅  runs
   contoso.onmicrosoft.com  collector mailbox  ✅  runs; optional: ReportSettings.Read.All would add …
   contoso.onmicrosoft.com  collector onedrive ✅  runs (source report); optional: Files.Read.All would add …
   contoso.onmicrosoft.com  collector servicehealth ➖  skipped: missing ServiceHealth.Read.All
   contoso.onmicrosoft.com  reports            ➖  ReportSettings.Read.All not granted: concealment is inferred from report data
   contoso.onmicrosoft.com  signin             ✅  signInActivity available
   ```

   ❌ (exit code 1) means something must be fixed: no token (with the mapped reason), a
   collector listed explicitly in `collectors:` that cannot run, concealed report names, or
   signInActivity refused. `➖` is informational.

## Migrating from the Zabbix templates

Per client, the Zabbix templates hold the app registration's IDs and secret in macros:
`{$MS.GRAPH.TENANT.ID}`, `{$MS.GRAPH.CLIENT.ID}` and `{$MS.GRAPH.CLIENT.SECRET}` (plus
`{$MS.GRAPH.DAYS.BACK}`, `{$MS.GRAPH.MAILBOX.SIZE.GB}`, `{$MS.GRAPH.ONEDRIVE.SIZE.GB}`,
`{$MAILS.SENT}`, `{$MAILS.RECEIVED}`, `{$MS.GRAPH.DAYS.INACTIVE}`). They are set on the
per-client **template** copies (as in the exports), on **hosts**, or globally; a host macro
overrides a template macro, which overrides a global one.

### 1. Find each client's IDs and secret

The Zabbix UI and API never show `SECRET_TEXT` values, but the database stores them in
clear text. The macro `type` column says how the value is stored:

| `type` | meaning | where the secret is |
|---|---|---|
| 0 | plain text | in `value`, in clear text. **The Liberty template and several m365.yaml templates store `{$MS.GRAPH.CLIENT.SECRET}` this way.** |
| 1 | secret text | in `value`, in clear text (only hidden in the UI/API) |
| 2 | vault | `value` is a vault path (`path/to/secret:key`); read the secret from that vault |

Templates are rows in `hosts` with `status = 3`. The listing below masks the secret by
macro **name**, whatever its type, so nothing secret reaches the screen. MySQL / MariaDB and
PostgreSQL (same SQL):

```sql
-- template and host macros
SELECT h.host, CASE h.status WHEN 3 THEN 'template' ELSE 'host' END AS kind,
       hm.macro, hm.type,
       CASE WHEN hm.macro = '{$MS.GRAPH.CLIENT.SECRET}' THEN '<secret>' ELSE hm.value END AS value
FROM hostmacro hm
JOIN hosts h ON h.hostid = hm.hostid
WHERE hm.macro IN ('{$MS.GRAPH.TENANT.ID}', '{$MS.GRAPH.CLIENT.ID}', '{$MS.GRAPH.CLIENT.SECRET}',
                   '{$MS.GRAPH.DAYS.BACK}', '{$MAILS.SENT}', '{$MAILS.RECEIVED}',
                   '{$MS.GRAPH.MAILBOX.SIZE.GB}', '{$MS.GRAPH.ONEDRIVE.SIZE.GB}',
                   '{$MS.GRAPH.DAYS.INACTIVE}')
ORDER BY h.host, hm.macro;

-- global macros
SELECT macro, type,
       CASE WHEN macro = '{$MS.GRAPH.CLIENT.SECRET}' THEN '<secret>' ELSE value END AS value
FROM globalmacro
WHERE macro LIKE '{$MS.GRAPH.%' OR macro LIKE '{$MAILS.%';
```

Note each client's tenant ID, client ID, and which host, template or global macro holds the
secret and with which `type`.

### 2. Write the secret to a file the container can read (never on screen)

The collector runs as uid/gid **10001** and reads `deploy/secrets/tenants/` through a bind
mount, so the directory must be traversable by group 10001 and the file readable by uid
10001. Create the directory **before** setting a restrictive umask (`umask 077` alone would
make it 0700 and unreadable for the container):

```sh
sudo install -d -m 0750 -o "$(id -un)" -g 10001 deploy/secrets/tenants
umask 077
```

Then read one secret straight into a file, so it never appears in the terminal or in shell
history. Put the SQL in a file (the `{$…}` macro names would otherwise need shell escaping),
and pick the variant that matches where the secret is:

```sh
cat > /tmp/secret.sql <<'SQL'
-- host or template macro (type 0 = plain text or 1 = secret text)
SELECT hm.value FROM hostmacro hm JOIN hosts h ON h.hostid = hm.hostid
WHERE h.host = 'M365 send receive audit'   -- the template/host that holds the client's secret
  AND hm.macro = '{$MS.GRAPH.CLIENT.SECRET}' AND hm.type IN (0, 1);
SQL
# or, for a global macro:
#   SELECT value FROM globalmacro
#   WHERE macro = '{$MS.GRAPH.CLIENT.SECRET}' AND type IN (0, 1);

# MySQL / MariaDB (--raw: no escaping of backslashes or tabs in the value)
mysql --batch --raw --skip-column-names -u zabbix -p zabbix < /tmp/secret.sql \
  | tr -d '\n' > deploy/secrets/tenants/contoso_client_secret
# PostgreSQL
psql -At -U zabbix -d zabbix -f /tmp/secret.sql \
  | tr -d '\n' > deploy/secrets/tenants/contoso_client_secret
rm /tmp/secret.sql
test -s deploy/secrets/tenants/contoso_client_secret \
  && echo "written, $(wc -c < deploy/secrets/tenants/contoso_client_secret) bytes" \
  || echo "EMPTY: wrong host name, macro type 2 (vault), or the secret is a global macro"
sudo chown 10001:10001 deploy/secrets/tenants/contoso_client_secret
sudo chmod 0400 deploy/secrets/tenants/contoso_client_secret
```

Check that the container can read it (prints the byte count, not the secret):

```sh
docker compose -f deploy/docker-compose.yml exec collector \
  python -c "print(len(open('/run/m365c-secrets/contoso_client_secret','rb').read()))"
```

If it cannot, the tenant reports `reason="secret_unavailable"` and the log names the file
and says that uid 10001 must be able to read it and traverse its directories.

For a vault macro (`type = 2`), read the secret from that vault instead, or point m365c at
it directly: `client_secret: vault:<mount>/<path>#<key>` (HashiCorp KV v2) or
`keyvault:https://<kv>.vault.azure.net/<name>`. `env:VAR` works too (Docker `env_file`,
systemd `EnvironmentFile=`), but environment variables are visible to anyone who can inspect
the container; prefer files or a vault.

Look up the secret's expiry in the client's app registration (*Certificates & secrets*) for
`--client-secret-expires`. The secret has been stored in clear text in the Zabbix database:
consider rotating it once the migration is done (create a new secret, replace the file,
delete the old secret in Entra).

### 3. Add and verify the tenant

```sh
m365c tenant add --tenant-id <tenant id> --name <domain> --customer <slug> \
  --auth client-secret --client-id <client id> \
  --client-secret-ref file:/run/m365c-secrets/<slug>_client_secret \
  --client-secret-expires <YYYY-MM-DD>
m365c tenant verify <slug>
```

If the template used another `{$MS.GRAPH.DAYS.BACK}` than `D7`, set it on the tenant
(`reports: {period: D30}` in `tenants.yaml`). Put the size/traffic macros in
`thresholds.yaml` under `customers.<slug>`.

### 4. Compare side by side (run both for a few days)

| Zabbix item (per discovered host) | m365c query |
|---|---|
| `sku.total.licenses[…]` / `sku.used.licenses[…]` / `available.licenses[…]` | `m365_license_total{sku=…}` / `m365_license_consumed` / `m365_license_available` |
| Disabled user: `Disabled.department`, `Disabled.licenses`, `Disabled.lastactivity` | `m365_user_info{enabled="false",licensed="true"}`, `m365_user_license{upn=…}`, `m365_user_last_activity_timestamp{upn=…}` (Zabbix used *any* sign-in attempt; expect m365c's date to be equal or older) |
| Mailbox: `mail.used.storage`, `mail.last.activity` | `m365_mailbox_used_bytes{upn=…}`, `m365_mailbox_last_activity_timestamp{upn=…}` |
| Discovered mailboxes (≥ `{$MS.GRAPH.MAILBOX.SIZE.GB}`) | `m365_mailbox_used_bytes >= on(customer,tenant) group_left() (m365_threshold{check="mailbox_size_gb_min"} * 1073741824)` |
| *M365 Mailbox Size – F1* / *– G3* | `m365_mailbox_used_bytes * on(customer,tenant,upn) group_left() (max by (customer,tenant,upn) (m365_user_license{sku="M365_F1_GOV"}) unless on(customer,tenant,upn) m365_user_license{sku="M365_G3_GOV"})` (swap SKUs for G3) |
| OneDrive: `onedrive.used`, `onedrive.total`, `onedrive.filecount`, `onedrive.lastactivity` | `m365_onedrive_used_bytes`, `m365_onedrive_total_bytes`, `m365_onedrive_file_count`, `m365_onedrive_last_activity_timestamp` |
| `onedrive.free.storage`, `onedrive.pused.storage` | `m365_onedrive_total_bytes - m365_onedrive_used_bytes`, `m365_onedrive_used_bytes / m365_onedrive_total_bytes` |
| Inactivity audit: `mail.last.activity` older than `{$MS.GRAPH.DAYS.INACTIVE}` (D30) | Grafana table *Mailboxes inactive longer than inactive_days (mailbox report)*: `(time() - m365_mailbox_last_activity_timestamp) / 86400 > on(customer,tenant) group_left() m365_threshold{check="inactive_days"}`, plus never-active mailboxes (`m365_mailbox_info unless on(customer,tenant,upn) m365_mailbox_last_activity_timestamp`). No P1 needed |
| `sent.mails`, `recieved.mails` | `m365_mail_sent{upn=…}`, `m365_mail_received{upn=…}` (same `period`) |
| Discovered high-traffic users | `M365HighMailTraffic` alert / "Emails sent/received" table |

Expect m365c to list **more** users than Zabbix for large tenants: the templates read only
the first page of `/users` and of each report.

### 5. Turn off the Zabbix templates

When the numbers match and `m365_collector_success` has been 1 for every parity collector
for a few days: in Zabbix, disable the client's M365 hosts (or unlink the M365 templates
from them and delete the discovered hosts), then remove the `{$MS.GRAPH.CLIENT.SECRET}`
macros so the secret no longer sits in the Zabbix database. Keep the Zabbix history if you
need it for comparison; m365c does not import it.

## Configuration reference

Three files in the config directory (`--config-dir`, `M365C_CONFIG_DIR`, default `./config`;
`/etc/m365c` in the container). Examples are in `config/*.example.yaml`; real files are
git-ignored. All config is validated at startup; errors name the file and the key, never a
secret value.

**`settings.yaml`** (read at startup)

| key | default | meaning |
|---|---|---|
| `app.method` | `certificate` | default auth method: `certificate` or `client_secret` |
| `app.client_id` | required | default Application (client) ID |
| `app.client_secret` | none | secret reference, required if `app.method: client_secret` |
| `app.client_secret_expires` | none | expiry date of that secret (`YYYY-MM-DD`) |
| `secrets.cert` | none | reference of the default PEM (key + cert) or PKCS#12; needed only if a tenant uses it |
| `secrets.cert_password` | none | reference of the key/PFX password, if encrypted |
| `reports.period` | `D7` | report period for mailbox, mailtraffic and onedrive (report): `D7`, `D30`, `D90`, `D180` |
| `onedrive.source` | `report` | `report`, `quota` or `auto` |
| `http.max_concurrency` | 20 | concurrent Graph requests, all tenants |
| `http.per_tenant_concurrency` | 4 | concurrent Graph requests per tenant |
| `http.timeout_s` | 60 | per request |
| `http.max_retries` | 5 | for 429/500/502/503/504 and network errors |
| `server.host` / `server.port` | `0.0.0.0` / `9365` | HTTP listener |
| `cache.persist_dir` | none | directory for JSON snapshots, so restarts start with data |
| `reload_interval` | `30s` | how often tenants/thresholds files are checked |
| `intervals.<collector>` | collector default | e.g. `mailbox: 12h` (`s`, `m`, `h`, `d`) |

**`tenants.yaml`** (hot reloaded: added tenants start collecting, removed or disabled ones
are dropped from the cache and `/metrics`, including their Graph counters; a changed `auth:`
block gets a new credential). A change is applied once the file has been unchanged for one
poll, so half-written saves are not picked up. An invalid, unreadable or empty file, or one
whose auth cannot be resolved (e.g. certificate auth without any certificate), is logged and
ignored, and the last good configuration stays. The `tenants:` key is required: write
`tenants: []` to run with no tenants. `m365c tenant add` validates first, then replaces the
file atomically (comments are not kept).

```yaml
tenants:
  - id: 11111111-1111-1111-1111-111111111111
    name: contoso.onmicrosoft.com   # `tenant` label
    customer: contoso               # `customer` label, lowercase slug
    auth:                           # optional; default: settings.yaml app:
      method: client_secret         # client_secret | certificate
      client_id: 33333333-3333-3333-3333-333333333333
      client_secret: file:/run/m365c-secrets/contoso_client_secret
      client_secret_expires: 2027-03-01
      # certificate: cert: <ref>, cert_password: <ref> (both optional)
    reports:
      period: D30                   # optional; default: settings reports.period
    onedrive:
      source: report                # optional; default: settings onedrive.source
    collectors: all                 # or [license, users, ...]
    enabled: true
```

**`thresholds.yaml`** (hot reloaded). Exported as `m365_threshold{customer,tenant,check}`;
alert rules and dashboards join against it, so changing a threshold needs no Prometheus
reload.

```yaml
defaults:
  license_util_warn: 0.95
  storage_used_warn: 0.90
  inactive_days: 30          # {$MS.GRAPH.DAYS.INACTIVE}
  mail_sent_max: 800         # {$MAILS.SENT}, over the report period (old key mail_sent_max_7d accepted)
  mail_received_max: 800     # {$MAILS.RECEIVED} (old key mail_received_max_7d accepted)
  mailbox_size_gb_min: 10    # {$MS.GRAPH.MAILBOX.SIZE.GB}: dashboard filter
  onedrive_size_gb_min: 1    # {$MS.GRAPH.ONEDRIVE.SIZE.GB}: dashboard filter
customers:
  contoso:
    storage_used_warn: 0.85   # overrides the default for this customer only
```

**Environment overrides.** Every value can be overridden with `M365C_` and `__` for
nesting: `M365C_HTTP__MAX_CONCURRENCY=10`, `M365C_SECRETS__CERT=file:/run/secrets/x`,
`M365C_REPORTS__PERIOD=D30`, `M365C_CACHE__PERSIST_DIR=/var/lib/m365c/cache`,
`M365C_TENANTS='[{"id": …}]'` (JSON), `M365C_THRESHOLDS_DEFAULTS__INACTIVE_DAYS=45`.
Environment beats YAML, so a `M365C_TENANTS` or `M365C_THRESHOLDS_*` variable makes edits to
the hot-reloaded file appear to do nothing; the service logs a warning at startup when one is
set. Logging: `M365C_LOG_LEVEL` (default `INFO`).

## Secret backends

Secrets are loaded into memory only, never written to disk or logged; every loaded value is
also registered with the log redactor, which scrubs it (and its URL-encoded forms) from any
log line. A certificate is parsed and checked when loaded (key present, matches the
certificate, password right). The default certificate's expiry is logged at startup
(warning within 30 days, error if expired) and exported as `m365_cert_expiry_timestamp`.

| reference | backend | needs |
|---|---|---|
| `file:/run/secrets/m365c_cert` | file | nothing (Compose secrets, bind mounts); a trailing newline is stripped from client secrets |
| `env:M365C_CONTOSO_SECRET` | environment variable | nothing (Docker `env_file`, systemd `EnvironmentFile=`) |
| `vault:secret/m365c/cert#pem` | HashiCorp Vault KV v2: mount `secret`, path `m365c/cert`, key `pem` | `pip install m365c[vault]`; `VAULT_ADDR`; AppRole (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`) or Kubernetes (`VAULT_K8S_ROLE`, optional `VAULT_K8S_TOKEN_PATH`, `VAULT_K8S_MOUNT`) or `VAULT_TOKEN`; optional `VAULT_NAMESPACE` |
| `keyvault:https://<kv>.vault.azure.net/<name>` | Azure Key Vault secret (a Key Vault *certificate* is readable as a secret of the same name; PKCS#12 is base64-decoded automatically) | `pip install m365c[azure]`; `DefaultAzureCredential` (managed identity, workload identity, `AZURE_*` env) |

The container image includes both extras.

## Certificate rotation

No downtime, no client involvement (consent is per app, not per certificate):

1. Create a new key pair (see above) and **upload the new certificate** to the app
   registration. Keep the old one; both are now valid.
2. **Switch the secret**: replace `deploy/secrets/m365c_cert.pem` (or the Vault/Key Vault
   value). A `file:` certificate is picked up within a minute per tenant; restart the
   collector (`docker compose -f deploy/docker-compose.yml up -d --force-recreate collector`)
   to refresh `m365_cert_expiry_timestamp` and the startup check.
3. **Verify**: `m365c tenant verify <customer>` for a few tenants, and check in Prometheus
   that `min(m365_tenant_auth_ok) == 1` (the `M365TenantAuthFailed` alert stays quiet).
4. **Remove the old certificate** from the app registration.

Client secrets rotate the same way: add a new secret in the client's app registration,
replace the tenant's secret file (picked up within a minute, no restart), update
`auth.client_secret_expires`, then delete the old secret in Entra.

## Deploy

```sh
cp config/settings.example.yaml config/settings.yaml      # set app.client_id
cp config/thresholds.example.yaml config/thresholds.yaml
m365c -c config tenant add ...                             # or copy tenants.example.yaml
# secrets (git-ignored), see deploy/secrets/README.md
cp m365c_cert.pem deploy/secrets/m365c_cert.pem            # only if a tenant uses the default cert
#   per-tenant client secrets: directory first (group 10001 must traverse it), then the files:
#   sudo install -d -m 0750 -o "$(id -un)" -g 10001 deploy/secrets/tenants
#   deploy/secrets/tenants/<customer>_client_secret (owner 10001, mode 0400), see Migrating
openssl rand -base64 24 > deploy/secrets/grafana_admin_password
printf '%s' 'smtp-password' > deploy/secrets/alertmanager_smtp_password
printf '%s' 'https://hooks.example/...' > deploy/secrets/alertmanager_webhook_contoso
docker compose -f deploy/docker-compose.yml up -d --build
```

- Collector: `:9365/metrics`, `/healthz` (process up), `/readyz` (config loaded and at least
  one collector run attempted, or no tenants configured). Not published on the host by
  default. `/metrics` is rendered off the event loop. The image healthcheck follows
  `M365C_SERVER__PORT`. Per-tenant client secrets are bind-mounted from
  `deploy/secrets/tenants/` at `/run/m365c-secrets/`. With client-secret tenants only, remove
  the `M365C_SECRETS__CERT` line and the `m365c_cert` secret from the Compose file.
- Prometheus `127.0.0.1:9090` (scrapes every 60s, rules in `deploy/prometheus/rules/m365.yml`),
  Alertmanager `127.0.0.1:9093`, Grafana `127.0.0.1:3000` (dashboard *Microsoft 365 overview*,
  `customer` and `tenant` variables, with a **Template parity** row). Put a reverse proxy
  with TLS in front for remote access. The **Licences** row has the Zabbix widget **M365
  Available License Count**: one card per tenant and SKU (named by product, or the SKU part
  number), fewest available first. Cards are red at ≤ 0 (exhausted or over-assigned),
  orange at 1–5 and green above 5. SKUs with no enabled units (free/viral SKUs such as
  `FLOW_FREE`) are hidden there, unlike in Zabbix; the *Licences* table, which also shows
  total and consumed, still lists them.
- Alloy ships container logs labelled `logs.netwatch/ship=true` to Loki, with `level`,
  `customer` and `collector` extracted as labels. Alloy talks to a `docker-socket-proxy` that
  only allows `GET /containers/*` and `GET /networks`, not the Docker socket. The proxy sits
  on its own internal network shared only with Alloy, because container inspect output
  includes other containers' environment.

**Personal data.** Logs and the snapshot cache contain user principal names, and the cache
also holds display names, departments and licences (`*_info`, `m365_user_license`). With
`cache.persist_dir` set (on by default in Compose: the `m365c-cache` volume), they are stored
as JSON files, one per tenant and collector, mode 0600, replaced on every successful run and
deleted when the tenant or collector is removed (also for tenants removed while the service
was down). Loki keeps logs for 30 days. Treat both volumes as customer data:
`docker compose down -v` removes them.
- Alertmanager groups by `customer, alertname` and has an example per-customer route
  (email + webhook). All credentials are `*_file` references to Compose secrets.

### Alerts

| Alert | Fires when | Severity |
|---|---|---|
| `M365LicenseNearlyExhausted` | consumed/total > `license_util_warn` | warning |
| `M365LicenseExhausted` | available ≤ 0 and total > 0 | warning |
| `M365MailboxNearlyFull` / `M365OneDriveNearlyFull` | used/quota (OneDrive: allocated) > `storage_used_warn` | warning |
| `M365DisabledUserLicensed` | disabled users hold licences, for 1d | warning |
| `M365UserInactive` | licensed, enabled user past `inactive_days` since the last successful sign-in (`signin="stale"`) or never signed in with an older account (`signin="never"`); only where signInActivity is available | info |
| `M365HighMailTraffic` | sent or received over `mail_sent_max`/`mail_received_max` (`direction`, `period` labels) | warning |
| `M365ServiceDegraded` | a service is degraded/interrupted/investigating | warning |
| `M365AppCredentialExpiring` | an app secret/certificate in the tenant expires within 30 days (needs `Application.Read.All`) | warning |
| `M365ClientSecretExpiring` | the tenant's own client secret (`auth.client_secret_expires`) expires within 30 days or has expired | warning |
| `M365TenantAuthFailed` | token failure or Graph 401; `reason` label from `m365_tenant_auth_error` (`secret_expired`, `invalid_client_secret`, `secret_unavailable`, `no_roles`, `identity_unreachable`, …) with matching guidance | critical |
| `M365TenantCertificateExpiring` | a tenant's own certificate (`auth.cert`) expires within 30 days or has expired | warning |
| `M365CollectorSkipped` | an *expected* collector is skipped for a missing permission, for 1 h (`permission` label) | warning |
| `M365PermissionRevoked` | a Graph role that was in the tenant's token in the last 7 days is gone | warning |
| `M365PermissionMissing` | a collector got Graph 403 | warning |
| `M365ReportsConcealed` | report names concealed (setting or heuristic) | warning |
| `M365CollectorFailing` | last run failed, for 3h | warning |
| `M365CollectorStale` | no success for 2× the collector's interval | warning |
| `M365CollectorDown` | Prometheus cannot scrape the collector (`customer="netwatch"`) | critical |
| `M365CertificateExpiring` | the default certificate expires within 30 days (`customer="netwatch"`) | critical |

**What goes quiet when a collector is skipped by design** (e.g. for the legacy apps, which
have only the parity permissions): `M365ServiceDegraded` (servicehealth),
`M365AppCredentialExpiring` (appsecrets; use `M365ClientSecretExpiring`), secure-score
panels, and `M365UserInactive` without `AuditLog.Read.All` + Entra ID P1 (the mailbox-report
inactivity table still works). Skipped collectors never fire
`CollectorFailing`/`CollectorStale`; they are listed in the dashboard's "Collectors skipped"
table and by `m365_collector_enabled == 0`. Parity collectors that *stop* running because a
permission was lost are not quiet: see [Permission gating](#permission-gating).

Each alert has `customer`, `tenant` and a `summary` naming the user, SKU, app or service.
Alertmanager inhibits `CollectorFailing`/`CollectorStale`/`PermissionMissing`/
`CollectorSkipped`/`PermissionRevoked` while `TenantAuthFailed` fires for the same tenant.

**Validation runs in the test suite.** `tests/test_deploy.py` runs `promtool check rules`,
`promtool check config`, `promtool test rules` (a unit test per alert, firing and not firing,
in `deploy/prometheus/tests/m365_test.yml`), `amtool check-config`, and parses every
dashboard query with promtool. It uses a local promtool/amtool if one is installed,
otherwise the official images through Docker. Without either it skips locally, but **fails
when `CI` is set**. By hand:

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
m365c permissions [--parity | --all]          # required/optional per collector, parity set
m365c tenant add --tenant-id <guid> --name <domain> --customer <slug>
                 [--auth client-secret --client-id <guid> --client-secret-ref <ref>
                  [--client-secret-expires YYYY-MM-DD]]
                 [--auth certificate [--client-id <guid>] [--cert-ref <ref>] [--cert-password-ref <ref>]]
m365c tenant list
m365c tenant verify <customer|tenant-name|tenant-id>
m365c run --tenant <x> [--collector <name> ...] [--dry-run] [--json]
```

Global options: `-c/--config-dir`, `--log-level`. `run --dry-run` writes neither the cache
nor the persistence directory; skipped collectors are shown with the missing permission.
`serve`, `run` and `tenant verify` accept a hidden `--fixtures <dir>` that serves Graph from
fixture files with a fake token (holding every role), for offline demos:

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
uv run mypy                   # strict
docker compose -f deploy/docker-compose.yml config --quiet
```

On Windows machines where Application Control blocks the `pytest.exe`/`mypy.exe` shims,
use `uv run python -m pytest` and `uv run python -m mypy`.

Layout: `src/m365c/` (package), `tests/` (fixtures in `tests/fixtures/graph`, fake data
only; `tests/identity.py` is a fake Entra token endpoint for real azure-identity
credentials), `config/` (examples), `deploy/` (Compose stack, Prometheus rules,
Alertmanager, Grafana, Loki, Alloy), `docs/parity.md` (template parity matrix).

Adding a collector: subclass `BaseCollector` in `src/m365c/collectors/` with `name`,
`default_interval`, `required_permissions` (any-of groups), `optional_permissions`,
`metric_help` and `async collect(ctx)` (use `ctx.roles`, `ctx.options`, `ctx.granted()`),
register it in `collectors/__init__.py`, add fixtures and a test.

## Deviations from the handover specs

From the original build:

| Spec | Implementation | Why |
|---|---|---|
| Retry 429 and 503/504 | Also retries 500, 502 and network errors (same backoff) | Transient Graph errors; one blip shouldn't fail a daily run |
| `m365_reports_concealed` with `customer`, `tenant` | Extra `report` label (and now `source`) | Several collectors emit it; identical label sets would make Prometheus reject the scrape |
| `m365_tenant_auth_ok` 0 on any `GraphAuthError` | 0 on definitive token failures (tenant-wide) and Graph 401s (per collector); a 403 sets `m365_collector_permission_ok{collector}` 0 instead | A single refused permission is not "cannot authenticate"; a 401 must not be cleared by another collector's success (review 2 L10) |
| Last sign-in timestamp | `lastSuccessfulSignInDateTime` only, plus `m365_user_created_timestamp` | The other fields include failed attempts |
| `securescore` `$top=1` | `$top=7`, newest by `createdDateTime` | The order isn't documented |
| `M365LicenseExhausted` `available == 0`, critical | `available <= 0`, warning | Fully assigned SKUs are normal for small clients |
| `M365AppCredentialExpiring` `< 30d` | `0 < remaining < 30d` | Expired, never-removed secrets would alert for ever |
| `M365CollectorStale` "2× interval, or 2d" | Uses exported `m365_collector_interval_seconds` | Exact per collector |
| Failed runs wait a full interval | Retry backoff 5 min → 1 h | One transient failure no longer means a 24 h gap |
| Log shipping via the Docker socket | Via a read-only docker-socket-proxy | The socket is root-equivalent |
| Build backend unspecified | `uv_build` | hatchling's build env deadlocked uv's cache lock on the dev machine |
| — | Hidden `--fixtures` on `serve`, `run`, `tenant verify` | Offline demos and end-to-end stack tests |

From the parity refactor (`m365-collector-parity-handover.md`):

| Spec / previous behaviour | Implementation | Why |
|---|---|---|
| `_7d` metric names "must not lie": rename **or** add `period` everywhere | **Renamed** to `m365_mail_sent`, `m365_mail_received`, `m365_mail_read`, each with a `period` label; thresholds renamed to `mail_sent_max`/`mail_received_max` (old `_7d` keys still accepted, exported under the new `check` names); rules and dashboard updated. Other report metrics (sizes, last activity) are point-in-time and carry no period | One consistent scheme; only traffic counts depend on the period |
| OneDrive previously used the live `/drive` quota | Default `onedrive.source` is now `report` | Required by the spec (legacy apps lack `Files.Read.All`). Intentional behaviour change for existing configs; set `source: quota` (or `auto`) to keep the old data, including `m365_onedrive_deleted_bytes` |
| Last activity "from the mailbox report when the mailbox collector has run" | The `users` collector reads the `mailbox` collector's cached snapshot for the same tenant (read-only `ctx.peer_samples`); the PromQL `or` alternative is documented above | Small, explicit coupling; no extra Graph call |
| Concealment heuristic: "most values look like 32-character hex hashes" | No `@` in any UPN and **at least half** of them 32-hex | A report with two rows, one hashed, should count; rows without `@` are never exported anyway |
| `m365_tenant_permission`: 1 per granted role | Also 0 for the roles m365c uses that are *not* granted | Makes "missing" visible in dashboards; only roles m365c knows about are exported |
| `m365_collector_skipped{permission}` | Any-of alternatives joined with `|` (e.g. `Organization.Read.All|LicenseAssignment.Read.All`) | One series per unmet requirement group |
| — | `Directory.Read.All` is not counted as satisfying any group | Not in the spec's parity set; Graph docs list it as a higher-privileged alternative for `subscribedSkus`, so a tenant granted only that role would see `license` skipped |
| Default certificate optional when unused | Checked at startup and **fatal** if a tenant uses it or there are no tenants (as before); otherwise only a warning | Keeps the old fail-fast behaviour where it matters, lets client-secret-only deployments start with the Compose file's cert line still present |
| `tenant verify` | Exit 1 also for a collector listed explicitly in `collectors:` that is skipped | It was asked for and cannot run; under `collectors: all` a skip is informational |
| Gating: skip collectors without permission | A token with **no** roles is an auth failure (`no_roles`); skipped *expected* collectors and revoked roles alert (`M365CollectorSkipped`, `M365PermissionRevoked`) | Review 2 H1: a silent skip turned loss of consent into loss of monitoring with no alert |
| Persistent identity-service errors were always "transient" | After 30 min they become `reason="identity_unreachable"` | Review 2 L5: a misconfigured proxy would otherwise never show as an auth problem |
| Client secret expiry alert `< 30 days` | Includes already expired secrets | Configured on purpose; unlike tenant app credentials, they must be renewed |
| `m365c permissions` printed a flat list | Prints a table plus the parity set; `--all` gives the old flat list, `--parity` the four lines | Required vs optional per collector |
| Per-tenant certificates | Supported (`auth.cert`); exported as `m365_tenant_cert_expiry_timestamp{customer,tenant,thumbprint}` with `M365TenantCertificateExpiring`; the default certificate keeps `m365_cert_expiry_timestamp` | A separate metric, so the default certificate's alert (labelled `customer="netwatch"`) is not duplicated per tenant |
| `CertificateTokenProvider` (internal API) | Replaced by `TenantTokenProvider` | Per-tenant methods and secrets |
| Optional `m365c zabbix parity-check` | **Not implemented** | Optional; see Branch status |

Graph and Entra documentation checks (October 2026) and surprises:

- `getOneDriveUsageAccountDetail` (v1.0) CSV now starts with a **`Site URL`** column after
  *Report Refresh Date*; parsing is by header name, so order does not matter.
- The v1.0 `getMailboxUsageDetail` page lists no `Recipient Type` column (the fixture, from
  older output, has one; it is not used). The mailbox report's `period` does not change
  sizes, only the activity window.
- `subscribedSkus`: least privileged `LicenseAssignment.Read.All`; higher: `Organization.Read.All`,
  `Directory.Read.All` (see the table above).
- All three reports accept `D7`, `D30`, `D90`, `D180` and answer 302 to a pre-authenticated
  download URL; the email-activity report covers 28 days for `date=` queries (not used).
- The Entra token endpoint returns `error`, `error_description` and `error_codes`; the
  description is free text and must not be used programmatically, so m365c maps on the
  AADSTS code only. azure-identity logs the raw description at WARNING on failure, which is
  why its logger is raised to ERROR.
- `CertificateCredential` requires an **RSA** key (RS256); EC keys pass m365c's own check but
  fail in azure-identity.

## Needs a real tenant

Everything above is verified offline (unit tests, real azure-identity credentials against a
fake token endpoint, promtool/amtool, Compose validation). These need real tenants:

1. A migrated client (client secret): `m365c tenant verify` shows `client_secret`, the four
   parity roles in the token, all five parity collectors running and the three others
   skipped. Compare the numbers with the Zabbix items for a few days (checklist above).
2. Entra error mapping with real responses: a wrong secret (`AADSTS7000215`), an expired one
   (`AADSTS7000222`), a wrong client ID (`AADSTS700016`), a wrong tenant (`AADSTS90002`),
   missing consent (`AADSTS65001`): reason label, alert text, and that nothing secret shows
   up in logs.
3. Secret rotation: replace a secret file; the next token uses it within about a minute and
   the old credential is closed. Grant a new permission (e.g. `ServiceHealth.Read.All`) and
   confirm the collector starts within an hour, with no restart.
4. Certificate method (shared app): token, rotation with two certificates, an
   `AADSTS700027`-style error for a removed certificate.
5. signInActivity without P1: the exact 403 code, the fallback fires once,
   `m365_tenant_feature` = 0, `m365_user_last_activity_timestamp` from the mailbox report.
6. Reports: the 302 flow, BOM, exact CSV headers (including OneDrive's `Site URL`), the
   concealment heuristic against a concealed tenant without `ReportSettings.Read.All`, and
   the `/admin/reportSettings` shape where the permission is granted.
7. OneDrive report vs quota numbers on the same tenant (allocated vs quota total; daily vs
   live), and quota timing on the largest tenant.
8. The agent-user filter on v1.0 (200 or 400?), and page size/throttling with
   signInActivity on large tenants.
9. healthOverviews `status` casing; secureScores order; appsecrets `endDateTime`.
10. All collectors at once: `m365_graph_throttled_total` and `Retry-After` values.

## Branch status

Branch `feature/client-secret-template-parity` (from `main` at `74492f1`; not merged,
not pushed). All commits are green: ruff, ruff format, mypy (strict), pytest (offline),
promtool check/test, amtool, `docker compose config`.

**Done**

- Phase 1: parity matrix (`docs/parity.md`).
- Phase 2: per-tenant auth (`client_secret`, `certificate`, pluggable methods), `env:`
  backend, reference-only secrets with non-echoing validation, lazy per-tenant secrets,
  credential cache keyed by secret fingerprint with file rotation, Entra error mapping,
  `m365_tenant_auth_error`, `m365_client_secret_expiry_timestamp`, leak tests.
- Phases 3 and 4 (one commit, the changes were coupled): permission gating with any-of
  groups, skip/enable/permission metrics, `tenant verify` and `permissions`; user licences,
  OneDrive report source, report period, concealment heuristic, mailbox info and deleted
  count, zero-count mail traffic, new thresholds; the end-to-end acceptance test.
- Phase 5: Grafana "Template parity" row, rules (`M365ClientSecretExpiring`, `reason` on
  `M365TenantAuthFailed`, renamed metrics), promtool tests.
- Phase 6: `tenant add --auth …` (references only), migration guide.
- Phase 7: this README.
- Review 2 fixes (`m365-collector-review-2.md`): every finding H1–H2, M1–M3, L1–L12 and
  N1–N5. See "Review 2" below.

**Left**

- Optional `m365c zabbix parity-check` (Zabbix API + Prometheus diff per UPN): not started.
  Sketch: read item values with `item.get`/`history.get` for the hosts created by the
  discovery rules, map them with the checklist in "Migrating from the Zabbix templates",
  query Prometheus `/api/v1/query` for the same UPNs, print differences; mark experimental.
- Real-tenant validation (list above), including the review 2 drills: wrong secret, removed
  `Reports.Read.All` (expect `M365PermissionRevoked` and `M365CollectorSkipped`), revoked
  consent (expect `reason="no_roles"`), rotated secret file.

**Resume**

```sh
git switch feature/client-secret-template-parity
uv sync --all-extras
uv run python -m pytest -q && uv run ruff check && uv run ruff format --check && uv run python -m mypy
```

Key files: `src/m365c/auth.py` (methods, `TenantTokenProvider`, error mapping),
`src/m365c/health.py` (auth and skip state),
`src/m365c/config.py` (`TenantAuth`, `resolve_auth`, thresholds), `src/m365c/scheduler.py`
(gating), `src/m365c/collectors/` (`required_permissions`/`optional_permissions`),
`tests/test_parity_e2e.py` (acceptance), `tests/test_secret_leaks.py`,
`tests/test_review2.py` (review 2 regressions). The full suite runs in about 30 s (the fake
identity endpoint is used with `retry_total=0`; promtool through Docker is most of it).

### Review 2

| ID | Fix |
|---|---|
| H1 | No-roles token → `TokenError(reason="no_roles")`; `m365_collector_expected`; alerts `M365CollectorSkipped`, `M365PermissionRevoked`; promtool tests |
| H2 | Migration steps create `deploy/secrets/tenants` as `0750 op:10001` before `umask 077`; container read check; permission-denied message names uid and fix |
| M1 | Grafana table "Mailboxes inactive longer than inactive_days (mailbox report)", including never-active mailboxes; parity matrix and comparison table updated |
| M2 | Migration SQL masks by macro name, extracts `type IN (0, 1)`, has a `globalmacro` variant, uses `mysql --raw` |
| M3 | README: a JWT without roles is an auth error; only a non-JWT disables gating |
| L1 | Disabled-users widget uses `last_activity or mailbox_last_activity` in PromQL |
| L2 | `m365_report_rows_skipped{report,reason="no_upn"}` + one warning |
| L3 | GUID errors no longer echo the value |
| L4 | `_ready` closes a credential built for an entry that `configure()` replaced, and retries |
| L5 | Transient identity errors for 30 min → `identity_unreachable` |
| L6 | A token failure keeps a skipped collector skipped |
| L7 | `m365_tenant_cert_expiry_timestamp`, `M365TenantCertificateExpiring` |
| L8 | Examples show the parity settings; `main`'s examples are kept as `tests/fixtures/legacy-config` for the compatibility test |
| L9 | e2e over both licence roles; leak tests over all parity collectors, persisted cache files, `tenant add` options, 200-without-token, 500 echo, `invalid_request` echo; Vault/Key Vault tests; `retry_total=0` (suite 51 s → 30 s) |
| L10 | Graph 401s tracked per collector; token failures stay tenant-wide |
| L11 | Quota-mode per-drive 403s logged, not counted toward the 10 % failure ratio |
| L12 | docker-socket-proxy on an internal network shared only with Alloy |
| N1–N2 | Parity matrix: ten templates; OneDrive discovery filter described as an OR |
| N3 | `tenant add` checks duplicates before auth problems |
| N4 | Expired secrets/certificates are worded "has expired" |
| N5 | Panels for `m365_mailbox_deleted` and `m365_tenant_permission` |
