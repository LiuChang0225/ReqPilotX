# Private SaaS pilot boundary

The Web uses the existing server and shared extraction/delivery engine. Pair,
the portable plugin and the existing local mode keep their existing contracts
when `REQPILOT_SAAS_ENABLED=0`. OpenKB and other unreviewed APIs are excluded
from the SaaS surface until their object-level ownership has independent evidence.

## Accounts and sessions

- `REQPILOT_SAAS_ENABLED=1` requires an exact HTTPS origin, or a literal loopback
  HTTP origin for the private tunnel pilot, and a 32..512-character bootstrap invite
- Each account has a separate private project owner identity; no client actor,
  tenant header or project-owner field can grant authority
- Bootstrap and locally issued invitations are hash-only, expiring and single-use
  Account insertion and invitation consumption share one transaction; restarting
  does not renew a used or expired invite, and there is no automatic first-user role
- Passwords use salted scrypt N=131072, r=8, p=1, with one concurrent KDF per API
  process. Opaque session tokens are stored only as SHA-256 digests in PostgreSQL
- Sessions expire after eight hours by default. Production cookies are Secure,
  HttpOnly and SameSite=strict; every mutation requires the session's CSRF token
  and exact Origin. Logout revokes the server session, and login rotates it
- Email ownership verification, password reset, organization membership and
  browser-profile encryption of locally retained unsent drafts are not implemented

## Project, task and delivery ownership

Migration 0039 adds account/session/invitation/project-owner tables. Project row,
owner binding and creation-command receipt commit together. Browser command IDs
are namespaced by the trusted account on the server. Both project catalogs filter
by owner, while historical unowned projects remain inaccessible.

Every FastAPI business route inherits the global authorization dependency. The
pilot uses an explicit frozen `(route path, method)` list; a new route or familiar
tag cannot implicitly obtain access. Project/document/resource references in path,
query and JSON body are checked against trusted ownership and document bindings.
Global provider setup/probes, host capability changes, shutdown, legacy unbound
exports, plugin synchronization and unreviewed knowledge/evidence APIs are denied.
Public `/api/healthz` returns only shallow database status and `authentication=required`.

The PostgreSQL queue stamps immutable server-owned account/project metadata at
enqueue. WorkerRuntime restores and revalidates that scope before invoking a
handler. Native task metadata and provider persistent-state bindings also pin
the account; fixed project/document/source/tool-workspace guards remain enforced.
No API switches a global database or runtime container based on a browser tenant.

Engineer opinions and immutable versioned Excel still use the shared source,
baseline, business-basis and publication fences. The native host permits the
existing `review-attach-delivery` only for a trusted compiler-produced directory
and an explicit current-review hash. An unchanged initial review can therefore
verify/publish its own exported bytes without manufacturing a review revision.

The account gate unmounts private UI on logout/expiry and prevents late responses
from an old session clearing a newer login. Existing unsent browser drafts,
commands and caches are account-namespaced; local/Pair storage keys remain unchanged.
This is UI/session isolation, not encryption against another person with access
to the same browser profile or developer tools.

## Evidence and remaining gates

`scripts/test_backend_batches.py` records the complete discovery module list,
source fingerprint, module hashes and per-batch logs/exit counts, and only reports
complete coverage after reconciling the full list. Native PostgreSQL tests have
a separate opt-in entrypoint; the offline runner continues to deny network access.

`scripts/test_saas_postgres.py` runs real temporary PostgreSQL, actual account HTTP,
worker lease/queue execution, registered native tools, review/resume and immutable
HTTP XLSX. Its model transport is explicitly deterministic `httpx.MockTransport`
through the real SDK; no final snapshot is seeded and no live model is claimed.
CI runs this with the genuine wheel-derived plugin bound to its exact HEAD.

The private ECS profile ships prebuilt assets and wheels, uses local objects and
PostgreSQL, and remains loopback-only. Real model credentials/customer-data scope,
public TLS/ICP and dependency-license clearance remain separate gates. Missing
model configuration must produce a blocked/not-configured state, never mock fallback.
