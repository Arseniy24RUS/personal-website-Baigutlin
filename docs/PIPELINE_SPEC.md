# Data pipeline

1. Validate profile identity and copy tracked files into an isolated candidate.
2. Collect open bibliographic sources, eLibrary, Scopus, WoS and media for the configured person.
3. Rebuild publication metadata while retaining reviewed fields, prior records and dated observations.
4. Validate retention, JSON/SEO and RU/EN browser behavior.
5. Reconcile against latest main and publish only allowed data/image paths; never force push.
6. Verify deployed JSON/assets and report upstream availability independently of publication success.

IT discovery is a separate weekly job with the same repository write lock. It scans public repositories of `Danil-phy-cmp-120`; existing cards remain immutable and new cards require complete bilingual content and a local preview. The six original repository cards remain in `repositories.json` as the legacy source and are migrated into `resources.json` with numeric GitHub identity aliases.

The original biography, 63 publications, historical metrics, 9 media records, 7 diploma records, repository cards, domain and webmaster verification files form the migration baseline. Existing static HTML remains a fallback; client renderers consume validated JSON on both languages.
