# Autonomous refresh

The weekly workflow runs Monday at 05:47 UTC (08:47 Moscow). It uses the existing
home OpenVPN connection for every source and every network enrichment step.
Installation happens before the tunnel is opened. No local always-on agent is
installed. WoS can use an entitled Clarivate API before the browser collector;
the API plans and activation checks are described in [WoS API access](WOS_API.md).

## Credentials and network

Repository Actions secrets: `ELIBRARY_OPENVPN_CONFIG_B64`, `ELIBRARY_USERNAME`,
`ELIBRARY_PASSWORD`, `WOS_ORCID_USERNAME`, `WOS_ORCID_PASSWORD`, `SCOPUS_API_KEY`.
`SCOPUS_INST_TOKEN` is optional and only useful if Elsevier issues institutional
entitlement. A valid Search API key does not guarantee Author Retrieval access.

Browser collectors first restore the last confirmed encrypted session and verify
the account and author. If it has expired, they attempt ordinary login once.
Legacy cookie/storage secrets are not injected over the restored state. See
[browser session operations](BROWSER_SESSIONS.md) for the encryption key and
daily maintenance. MFA, human verification, account linking and changed forms
have distinct failure reasons; a snapshot is never a fresh successful login.

Collectors run under a dedicated runner account whose outbound traffic is
restricted to `tun0`. Direct IPv4/IPv6 traffic is rejected; DNS uses the tunnel.
The browser exit is checked against the private tunnel observation. Only this
ephemeral runner is configured; the home router itself is not modified.

Passwords, cookies, VPN configuration, session URLs and raw authenticated HTML
are not published. Temporary runtime state is cleaned after collection. Public
diagnostics contain status codes and allowlisted form-control metadata, not form
values, sessions or raw response headers.

## Data and failure behavior

Collection runs in an isolated copy. Public record identities, existing manual
text/translations and images survive failed or partial collection. Provider
metrics change only after a complete verified observation; unknown is not zero.
All galleries are additive: a new upload or broken input cannot erase old items.

Each provider reports `status`, `attempted_at`, `last_success_at`, `origin`,
`complete`, `record_count`, and `reason`. `generated_at` is only a build date.
Public JSON `source_health` and metric data retain observation dates. Source
diagnostics and raw provider identifiers are not rendered on visitor pages.
Metric metadata distinguishes official profile values from complete-search
estimates, including Scopus and citation-enabled WoS Starter results.

The media pipeline polls institution news lists, RSS, nested sitemaps and configured
sources. Article body identity checks understand inflected names and initials.
New candidates/backlogs/review queues persist across runs. Confirmed records
publish automatically; ambiguous matches remain in the JSON/CSV review queue.
Required institutional news takes priority over generic sitemap backlogs. The
initial scan uses a 90-day lookback wherever reliable dates are available;
undated candidates remain queued. Each discovery channel has reserved time, so
a failing RSS host cannot consume the sitemap or institution scan budget.
Existing reviewed entries win over automatic metadata, and failed image/translation
downloads keep previous values. English fallback explicitly marks translation pending.

The Argos RU–EN model and sentence splitter are provisioned and tested before the
VPN starts, with a bounded setup time and a shared model cache. Collection uses
offline translation only. Translation and metadata enrichment have separate time
limits and transactional rollback: their failure leaves collected Russian records,
existing translations and caches intact. `derived_steps.json` and the Actions
summary expose failures without blocking otherwise validated data publication.

One workflow owns scheduled publication. Legacy media/WoS workflow buttons call
the same workflow. Writers share a concurrency group. Concurrent data commits
are recombined on a clean checkout and revalidated; concurrent code/config changes
abort safely. There is no autostash/rebase of generated JSON and no forced push.

## Running and verification

Use **Refresh scientist portfolio data**, `dry_run=true`, for a nonpublishing live
test. `sources` may select `elibrary,wos,scopus,open,media`; empty means all.
The production publishing path requires the main branch and `dry_run=false`.

Unit/browser tests and `validate_retention.py --baseline-ref <commit>` are mandatory.
The guard compares identities, protected fields, required files and Git content
hashes, with normal Git line-ending handling. The original inventory is recorded
in `data/audit/pre_repair_inventory.json`.
Safe results can be published even when a provider is blocked; the final source
availability step fails separately and describes which source needs attention.
Sanitized diagnostic artifacts are retained for seven days. A Pages build is
requested after data commits and public JSON hashes must match the candidate.

Local checks:

```text
python -m unittest discover -s tests/unit -v
python scripts/check_seo.py
python scripts/validate_retention.py --baseline-ref <baseline-commit>
npm ci
npx playwright install
npm run test:e2e
```

To run the same browser checks against the deployed site, set `E2E_BASE_URL` to
`https://baigutlin.ru` before running `npm run test:e2e`.

The repair baseline is `58d9b9ac0c95ca42a3885f7bd1a86284505dfa10`:67 publications,
20 media cards,13 RISS articles,57 diplomas,10 DPO entries and305 referenced local
assets. This patch adds the two September news items and an independently found
July academic-council mention. Import through GitHub Issues and IT-resource
automation remain outside this repair.
