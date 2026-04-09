# Control Tower Gates

- `release-foundation`: `p0-smoke`, `auth-login`, `feedback-roundtrip`, `health-readiness`
- `extraction-quality-loop`: `batch-report`, `oil-pump-benchmark`, `regression-suite`
- `rag-quality-loop`: `sample10`, `labeled-set`, `evidence-metrics`
- `product-kb`: `package-binding`, `dual-scope-retrieval`, `admin-curation`
- `angular-feasibility`: `shell-build`, `msal-login`, `migration-report`

- A queued track can start only after an active track finishes or is explicitly paused by decision.
- A failed gate blocks merge and should surface as a `RISK` or `OFF_PLAN` alert.

