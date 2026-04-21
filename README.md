# XRXS -> 263 Mail Sync

This project provides a Docker-friendly sync service for synchronizing departments and users from an XRXS-style HR OpenAPI into 263 enterprise mail.

## What is included

- A FastAPI service with `/healthz`, `/sync`, and optional `/sso-url` endpoints.
- A 263 JSON API client implemented from the `263云邮API开放接口文档V20200924.pdf` signature and request rules.
- A source adapter implemented for XRXS APIs that look like the 2haohr-style OpenAPI shape.
- A local SQLite state file to persist department and user mappings across runs.
- A background scheduler that runs sync jobs on a configurable interval.
- A startup sync that runs once whenever the container starts, before the hourly schedule continues.

## Current assumptions

The 263 side is implemented against the newer JSON API document `263云邮API开放接口文档V20200924.pdf`:

- base URL: `https://macom.263.net/api/mail/v2`
- request mode: `POST` JSON
- signing rule: remove `sign` and null fields, sort JSON keys, serialize compact JSON, append secret, then MD5 lowercase
- department methods: `/depts/get`, `/depts/create`, `/depts/update`, `/depts/delete`
- user methods: `/user/list`, `/user/create`, `/user/update`, `/user/modpwd`, `/user/delete`

Important:

- The 2020 document is not the old SOAP Web Services interface.
- If your MA page shows the newer API account and secret, those should be used with this JSON API client instead of the old WSDL account model.
- For many tenants, `MAIL263_ACCOUNT` is the API account shown in the 263 admin console, often the enterprise domain.
- The 263 server also checks caller IP. If you see `errcode=-1007`, add the Docker host egress IP to the 263 API whitelist first.

The XRXS side is implemented against the working Xinrenxinshi pattern already used in your other project:

- base URL: `https://api.xinrenxinshi.com`
- token endpoint: `POST /authorize/oauth/token`
- department endpoint: `POST /v5/department/list`
- employee endpoint: `POST /v5/employee/list`
- request signing: `HMAC-SHA1(AppSecret, raw_json_body)` then `base64` then URL-encode into the `sign` query parameter

If your tenant has custom routing or needs a `companyId` header, adjust these first:

- `.env`
- `app/xrxs_client.py`

The rest of the sync pipeline should not need major changes.

## Quick start

1. Copy `.env.example` to `.env`
2. Fill in your XRXS and 263 credentials
3. Start with dry run first:

```bash
cp .env.example .env
docker compose -f docker-compose.yml.example up --build
```

4. Trigger a sync:

```bash
curl -X POST http://127.0.0.1:8000/sync
```

5. After the returned stats look correct, change `DRY_RUN=false` and restart the container.
6. In production, set `SYNC_INTERVAL_MINUTES=60` and keep `TIMEZONE=Asia/Shanghai` with container `TZ=Asia/Shanghai`.

## Important configuration

- `SYNC_USERID_MODE=email_localpart`
  263 user APIs usually expect the local part without the domain for CRUD operations.
- `SYNC_NAME_PRESERVE_USERIDS=user1,user2`
  Optional comma-separated userids whose existing 263 display names should be preserved instead of overwritten by XRXS names.
- `SYNC_DISABLE_ABSENT_USERS=false`
  Unmatched existing 263 users are left untouched. Only users explicitly matched from XRXS are updated.
- `SYNC_DELETE_ABSENT_USERS=false`
  Leave this off unless you are very sure you want hard deletes.
- `MAIL263_GID=33`
  Default mailbox group ID from the 2020 PDF example. Change it if your 263 mailbox plan uses a different group.
- `MAIL263_RETRY_MAX_ATTEMPTS=5`
  Retries 263 requests when the API returns rate-limit error `-1042`.
- `MAIL263_REQUEST_INTERVAL_SECONDS=0.3`
  Adds a small delay between 263 API calls to reduce the chance of throttling.
- `MAIL263_PARTNER_ID`
  263 SSO partner id, required only when `/sso-url` is used.
- `MAIL263_AUTH_CORP_ID`
  263 SSO enterprise/corp id, required only when `/sso-url` is used.

## API endpoints

- `GET /healthz`
  Basic health check.
- `GET /config`
  Returns a safe subset of active configuration.
- `POST /sync`
  Runs a sync immediately.
- `GET /sso-url?email=user@example.com`
  Builds a 263 mail SSO link when SSO is enabled and configured.

## Data flow

1. Read departments and users from XRXS.
2. Read departments and users from 263.
3. Create or update departments first.
4. Create, update, disable, or delete users based on the configured policy.
5. Persist source-to-target mapping in `/data/sync-state.db`.

## Notes

- The service currently maps one primary department per user into 263, using the first mapped source department.
- Existing users are updated only for name, department, and enabled/disabled status. Title, mobile, and phone are not synchronized.
- Existing 263 users that do not match any XRXS account are skipped and not disabled or deleted.
- If a userid is listed in `SYNC_NAME_PRESERVE_USERIDS`, its current 263 display name is preserved during sync.
- Sync logs include a readable summary for each run, plus per-department/per-user details and 263 rate-limit retry records.
- The XRXS integration follows the Xinrenxinshi OAuth token and signed JSON request pattern.
- The 263 API supports multi-department users and this service sends `deptids` as an array.
- 263 names and titles are encoded as `base64(GBK)` as required by the 2020 PDF.
- 263 department and user operations follow the newer JSON sign rule from the 2020 PDF.
- Mailbox provisioning depends on whether 263 has enabled the required write-side mail admin APIs for the configured API account.

## Recommended rollout

1. Keep `DRY_RUN=true`
2. Run `/sync`
3. Inspect the returned stats and 263 admin console
4. Verify a few departments and users manually
5. Switch to `DRY_RUN=false`
6. Keep unmatched-user disable/delete off until the first few production runs are stable
