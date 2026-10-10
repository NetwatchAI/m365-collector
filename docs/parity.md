# Zabbix template parity matrix

Source of truth: the Zabbix 7.4 exports `../m365.yaml` and `../liberty_m365.yaml` (a per-client
variant). Both contain the same eleven templates. Liberty differs only where noted (an extra
`-org` copy of the mailbox discovery script, a `mails sent - B` debug item, and a quoting-aware
CSV parser in the send/receive audit that keeps rows with empty or zero counts).

Every template authenticates with a per-client app registration and **client secret**
(`{$MS.GRAPH.CLIENT.ID}`, `{$MS.GRAPH.CLIENT.SECRET}`, `{$MS.GRAPH.TENANT.ID}`). In m365c that is
`auth.method: client_secret` on the tenant (see README, "Authentication").

Legend: ✅ covered · ⚠️ intentionally different (reason given) · ❌ not covered (none).

All new metrics carry `customer` and `tenant` labels as well; they are left out below.
"Parity permissions" = `Reports.Read.All`, `User.Read.All`, `AuditLog.Read.All`,
`Organization.Read.All` *or* `LicenseAssignment.Read.All`.

## Template-wide macros

| Macro | m365c | Status |
|---|---|---|
| `{$MS.GRAPH.CLIENT.ID}` | tenant `auth.client_id` | ✅ |
| `{$MS.GRAPH.CLIENT.SECRET}` (SECRET_TEXT or plain) | tenant `auth.client_secret`: a secret *reference* (`file:`, `env:`, `vault:`, `keyvault:`); inline values are rejected | ✅ |
| `{$MS.GRAPH.TENANT.ID}` | tenant `id` | ✅ |
| `{$MS.GRAPH.DAYS.BACK}` (D7/D30/D90/D180) | `reports.period` in settings, per-tenant `reports.period` override; validated | ✅ |
| `{$MS.GRAPH.DAYS.INACTIVE}` (referenced, never defined in the templates) | threshold `inactive_days` → `m365_threshold{check="inactive_days"}` | ✅ |
| `{$MS.GRAPH.MAILBOX.SIZE.GB}` (10) | threshold `mailbox_size_gb_min` → `m365_threshold{check="mailbox_size_gb_min"}` | ⚠️ applied in dashboards, not at collection (every mailbox is collected) |
| `{$MS.GRAPH.ONEDRIVE.SIZE.GB}` (1) | threshold `onedrive_size_gb_min` | ⚠️ as above |
| `{$MAILS.SENT}` / `{$MAILS.RECEIVED}` (800) | thresholds `mail_sent_max` / `mail_received_max` (old `_7d` keys accepted) | ✅ |
| `{$CUSTOMER}` (mailbox discovery LLD macro) | `customer` label from `tenants.yaml` | ✅ |
| `{$NAME}`, `{$USERNAME}` (per discovered host) | `upn` label | ⚠️ one series per user instead of one Zabbix host per user |

## M365 license audit

| Template item | Kind | m365c metric | Status |
|---|---|---|---|
| `get.raw.data` (Ms Graph Usage, `GET /v1.0/subscribedSkus`) | Script | `license` collector, same endpoint | ✅ works with `Organization.Read.All` or `LicenseAssignment.Read.All` |
| `discover.SKU` (LLD `{#SKUPARTID}`, `{#SKUPARTNUMBER}`) | Discovery | `sku` label (`skuPartNumber`) | ✅ |
| `sku.total.licenses[{#SKUPARTID}]` (`prepaidUnits.enabled`) | Dependent prototype | `m365_license_total{sku}` | ✅ |
| `sku.used.licenses[{#SKUPARTID}]` (`consumedUnits`) | Dependent prototype | `m365_license_consumed{sku}` | ✅ |
| `available.licenses[{#SKUPARTID}]` (total − used) | Calculated prototype | `m365_license_available{sku}` | ✅ |

## M365 disabled users discovery by Netwatch / M365 disabled users by Netwatch

| Template item | Kind | m365c metric | Status |
|---|---|---|---|
| `get.raw.data.m365.disabled` (discovery: `GET /beta/users?$filter=accountEnabled eq false`, licensed only) | Script | `users` collector, `GET /v1.0/users` (all users, paged) | ⚠️ v1.0 instead of beta; all users are collected and the disabled+licensed subset is a PromQL filter |
| `m365.disabled.users.lld` (`{#USERNAME}`, `{#FORMATTEDNAME}`) | Discovery → host prototype per user | `m365_user_info{upn,enabled="false",licensed="true"}` | ⚠️ one series per user instead of one host |
| `get.raw.data.m365.disabled` (per-host: same query + `licenseDetails` per user) | Script | as above; licences from `assignedLicenses` | ⚠️ no `licenseDetails` call per user (one `subscribedSkus` call per run maps `skuId` → `skuPartNumber`) |
| `Disabled.department` | Dependent | `m365_user_info{department}` | ✅ |
| `Disabled.licenses` (comma-joined `skuPartNumber`) | Dependent | `m365_user_license{upn,sku,sku_id}` = 1 per licence | ✅ falls back to `sku=sku_id` if the SKU lookup is unavailable |
| `Disabled.lastactivity` (`signInActivity.lastSignInDateTime`) | Dependent | `m365_user_last_signin_timestamp{upn}` (needs `AuditLog.Read.All` + Entra ID P1) and `m365_user_last_activity_timestamp{upn}` | ⚠️ uses `lastSuccessfulSignInDateTime` (the template's field also counts failed attempts). Without sign-in data, `m365_user_last_activity_timestamp` falls back to the mailbox report's Last Activity Date |
| `isLicenseReconciliationNeeded`, `userId` (in raw JSON, never extracted to an item) | — | not exported | ⚠️ never used by an item or widget |
| `m365_users_disabled_licensed` (count; no template item) | — | extra | ✅ |

## M365 mailbox discovery / M365 mailbox size audit by Netwatch

| Template item | Kind | m365c metric | Status |
|---|---|---|---|
| `get.raw.data.m365.mailbox-discovery` (`getMailboxUsageDetail(period={$MS.GRAPH.DAYS.BACK})`, beta JSON, filtered ≥ size GB, + unused `licenseDetails` per user) | Script | `mailbox` collector, v1.0 CSV, configured period | ⚠️ size filter moved to the dashboard; the unused per-user `licenseDetails` call is dropped |
| `get.raw.data.m365.mailbox-discovery-org` (Liberty only; identical script, no consumer) | Script | — | ⚠️ duplicate with no dependent item; nothing to collect |
| `discover.mailboxes` (`{#USERPRINCIPALNAME}`, `{#FORMATTEDUSERPRINCIPALNAME}`, `{#ARCHIVED}`) | Discovery → host per mailbox | `m365_mailbox_info{upn,display_name,has_archive}` | ✅ |
| `get.raw.data.m365.mailbox` (per-host copy of the report) | Script | same report, once per tenant | ✅ |
| `mail.used.storage` (`storageUsedInBytes`, B) | Dependent | `m365_mailbox_used_bytes{upn}` | ✅ |
| `mail.last.activity` (`lastActivityDate`) | Dependent | `m365_mailbox_last_activity_timestamp{upn}` | ✅ |
| `hasArchive` | LLD macro | `m365_mailbox_has_archive{upn}`, `has_archive` label on `m365_mailbox_info` | ✅ |
| `get.raw.data.m365.mailbox_type` (`/users?$top=50` + report, hard-coded GOV SKU map) | Script | `m365_user_license` + `m365_mailbox_used_bytes` | ⚠️ the template read only the first 50 users and a fixed four-SKU map; m365c pages through all users and maps every SKU via `subscribedSkus` |
| `get.storage.gb.m365.f1` (*M365 Mailbox Size – F1*: F1 without G3) | Dependent (JS) | PromQL: `m365_mailbox_used_bytes * on(customer,tenant,upn) group_left() (max by (customer,tenant,upn) (m365_user_license{sku="M365_F1_GOV"}) unless on(customer,tenant,upn) m365_user_license{sku="M365_G3_GOV"})` | ⚠️ a query, not a stored item (works for any SKU pair) |
| `get.storage.gb.m365.g3` (*M365 Mailbox Size – G3*: G3 without F1) | Dependent (JS) | same, SKUs swapped | ⚠️ as above |
| Quota, item count (not in template) | — | `m365_mailbox_quota_bytes`, `m365_mailbox_items` | ✅ extra |
| Deleted mailboxes (rows with `isDeleted`) | — | `m365_mailbox_deleted` (count per tenant) | ✅ |

## M365 mailbox inactivity audit by Netwatch

| Template item | Kind | m365c metric | Status |
|---|---|---|---|
| `get.raw.data.m365.mailbox` (`getMailboxUsageDetail(period='D30')`, `lastActivityDate` older than `{$MS.GRAPH.DAYS.INACTIVE}`, incl. `isDeleted`) | Script | `mailbox` collector with configurable `reports.period` | ⚠️ the period is configurable (D30 to match), and inactivity is a query against `m365_threshold{check="inactive_days"}` instead of a collection-time filter |
| `mail.last.activity` | Dependent | `m365_mailbox_last_activity_timestamp{upn}` | ✅ |
| `mail.used.storage` | Dependent | `m365_mailbox_used_bytes{upn}` | ✅ |
| `itemCount`, `displayName` (raw JSON) | — | `m365_mailbox_items`, `m365_mailbox_info{display_name}` | ✅ |
| `isDeleted` rows | — | deleted rows skipped per user; counted in `m365_mailbox_deleted` | ✅ |

## M365 onedrive discovery / M365 onedrive size audit by Netwatch

| Template item | Kind | m365c metric | Status |
|---|---|---|---|
| `get.raw.data.m365.onedrive` (`getOneDriveUsageAccountDetail`, beta JSON, ≥ size GB, + `licenseDetails` per owner) | Script | `onedrive` collector, `source: report` (default): v1.0 CSV, configured period, `Reports.Read.All` only | ⚠️ size filter moved to the dashboard; per-owner `licenseDetails` dropped (`m365_user_license` has it) |
| `discover.onedrive` (`{#PRINCIPALNAME}`, `{#FORMATTEDNAME}`, filters `{#ISDELETED}=false`, `{#LASTACTIVITY}` not null) | Discovery → host per OneDrive | `m365_onedrive_info{upn,display_name}` | ⚠️ deleted rows are skipped; never-active OneDrives are kept (no last-activity sample) |
| `onedrive.used` (`storageUsedInBytes`) | Dependent | `m365_onedrive_used_bytes{upn}` | ✅ |
| `onedrive.total` (`storageAllocatedInBytes`) | Dependent | `m365_onedrive_total_bytes{upn}` | ✅ |
| `onedrive.filecount` | Dependent | `m365_onedrive_file_count{upn}` | ✅ |
| `onedrive.lastactivity` (`null` → `never`) | Dependent | `m365_onedrive_last_activity_timestamp{upn}` (absent = never) | ✅ |
| `onedrive.free.storage` (total − used) | Calculated | PromQL `m365_onedrive_total_bytes - m365_onedrive_used_bytes` | ✅ (dashboard) |
| `onedrive.pused.storage` / `onedrive.pfree.storage` (%) | Calculated | PromQL ratio (dashboard, `M365OneDriveNearlyFull`) | ✅ |
| `licenseType` (first `skuPartNumber`, raw JSON only) | — | `m365_user_license` | ✅ |

## M365 send receive audit discovery / M365 send receive audit

| Template item | Kind | m365c metric | Status |
|---|---|---|---|
| `get.raw.data.m365.send.receive.audit` (`getEmailActivityUserDetail(period={$MS.GRAPH.DAYS.BACK})`, CSV) | Script | `mailtraffic` collector, configured period, RFC 4180 CSV parser | ✅ the Liberty fix is kept: rows with zero or empty counts are exported as 0, and quoted fields with commas parse correctly |
| `discover.mailbox.resources-new` (users above `{$MAILS.SENT}` / `{$MAILS.RECEIVED}`) | Discovery → host per user | all users exported; `M365HighMailTraffic` and the dashboard filter on the thresholds | ⚠️ filtering is a query, not discovery |
| `sent.mails` | Dependent | `m365_mail_sent{upn,period}` | ✅ |
| `recieved.mails` | Dependent | `m365_mail_received{upn,period}` | ✅ |
| `sent.mailsb` (Liberty only, raw `$..sent` text) | Dependent | `m365_mail_sent` | ⚠️ debug copy of `sent.mails` |
| Read count (not in template) | — | `m365_mail_read{upn,period}` | ✅ extra |

## Dashboard widgets (Zabbix dashboards built on the items above)

| Widget | Grafana ("Template parity" row) | Status |
|---|---|---|
| Disabled users with licenses – ordered by last activity | table: UPN, display name, department, licences, last activity | ✅ |
| Mailbox activity by size | table: ≥ `mailbox_size_gb_min`, sorted by size, last activity | ✅ |
| Emails sent/received – last N days | table: sent and received above the thresholds | ✅ |
| OneDrive activity by size (GB) | table: ≥ `onedrive_size_gb_min`, used, allocated, % used, files, last activity | ✅ |

## Behaviour differences that apply to every template

- **Pagination.** The templates never followed `@odata.nextLink` (`/users?$top=50`, a single
  `/users?$filter=…` page), so large tenants were cut off. m365c follows every page.
- **Reports via CSV (v1.0)** instead of beta `?$format=application/json`. Same data, stable API.
- **One report download per tenant per interval**, instead of one per discovered host.
- **Concealed names.** The templates silently exported hashed IDs when the tenant concealed
  report names. m365c detects it (`/admin/reportSettings` when `ReportSettings.Read.All` is
  granted, otherwise from the report data) and exports `m365_reports_concealed` instead of
  hashed per-user series.
- **Errors.** Template errors were swallowed into `errors` in the JSON. m365c exposes
  `m365_collector_success`, `m365_tenant_auth_ok` and `m365_tenant_auth_error{reason}`.
