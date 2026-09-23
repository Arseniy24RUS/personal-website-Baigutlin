# IT resources: discovery and publication

The IT catalog is append-only. The weekly job adds new public repositories from
`Danil-phy-cmp-120`; it never rewrites or removes a published card, including cards
that it previously created automatically. The profile repository and the
portfolio repository itself are excluded. Existing external resources remain
in the catalog.

## Public presentation

`data/it/resources.json` feeds both `/it.html` and `/en/it.html`. The six original cards and their editorial fields/images are preserved in `data/it/retention_baseline.json`; the original `repositories.json` remains available as legacy input. Numeric GitHub repository IDs are mapped to these stable card IDs, so renaming a repository cannot duplicate an existing card. New repositories are appended after validation. No source-site featured projects or exclusions are copied.

## Preparing a new card

1. Enumerate every page of the owner's public repositories and skip known or
   explicitly excluded repository IDs.
2. Extract a meaningful project title and introduction from the README,
   falling back to the repository description. Preserve explicit Russian and
   English copy. Translate a missing language using verified offline Argos
   models, with separate RU-to-EN and EN-to-RU caches.
3. Resolve the application URL using an explicit override, GitHub Pages
   metadata, or an application link in the README/homepage. A repository that
   has no application links to its GitHub page.
4. Prefer a substantive README illustration over a screenshot. Resolve
   relative and reference-style images and HTML image elements; skip badges
   and small decorative icons. Store validated image files locally.
5. If a site has no usable README illustration, capture it in a fresh browser
   context at 1440 by 810 pixels, at least 60 seconds after DOMContentLoaded.
   A blank, failed or authentication page is not a valid project preview.
6. A repository with neither an illustration nor a website receives a neutral
   SVG preview in the portfolio's gray palette. A network failure is not
   evidence that a website or illustration does not exist.

Incomplete candidates remain in a persistent queue. A run captures at most
ten website screenshots; the remaining candidates are retried on the next
run. Published cards are frozen even if their README later changes. Deliberate
editorial revisions require a separate reviewed change, not a collector run.

## Scheduling and isolation

The separate **Add new IT resources** workflow runs on Mondays at 05:47 UTC
(08:47 Moscow), and supports manual dry runs. It uses the same
`portfolio-data-writer` concurrency group as the other data writers.

IT collection uses public GitHub data and the workflow's existing GitHub
token. It does not use the home VPN, WoS/eLibrary sessions or scientific API
credentials. Credentials are never sent to project websites or image hosts.
The IT scope does not rebuild scientific data or update their timestamps.

Candidates are staged before publication and merged with the latest `main`.
The latest published card always wins over a candidate with the same
identity. Automatic changes are restricted to `data/it/` and
`assets/it/thumbs/`; retained image files cannot be replaced. A concurrent
scientific publication must preserve IT additions, and vice versa.

For an isolated local collection, choose a new temporary staging directory:

```sh
python scripts/refresh_pipeline.py prepare --scope it --stage /tmp/portfolio-it-stage
python scripts/refresh_pipeline.py collect --scope it --stage /tmp/portfolio-it-stage
python scripts/refresh_pipeline.py health --scope it --stage /tmp/portfolio-it-stage
```

These commands do not publish. Use the workflow's `dry_run` input for a complete
cloud check including model provisioning and browser tests without a commit or
deployment. Keep `dry_run` false for the normal weekly publication.

## Acceptance and troubleshooting

Check the workflow's discovery, translation and image results, including its
pending count. Reports are operational data; they are not rendered on the
public pages. An unavailable provider must retain the catalog and pending
queue instead of reporting an empty successful refresh.

Before rollout, run the Python tests, SEO checks and IT browser tests. Validate
the initial ten cards against the recorded baseline and all published cards
against the current Git commit. After publication, verify both language pages,
the featured links, all local preview files, desktop/mobile layout and the
corresponding Pages build. Repeating the collection must add no duplicates or
change any published card. A manual dry run must not commit or deploy.
