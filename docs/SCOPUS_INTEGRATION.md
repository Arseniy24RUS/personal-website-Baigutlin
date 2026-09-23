# Scopus integration decision

## Core conclusion

A Scopus API key is an application/developer key, not a personal author-token. It is not intrinsically limited to one author profile. With an enabled Scopus API resource and sufficient entitlements, the same key can query other Scopus author IDs and Scopus Search queries. Practical access is constrained by:

- which Elsevier APIs are enabled for the key;
- default quotas and throttling;
- subscriber-only features and views;
- institutional IP authentication or Institutional Token;
- Elsevier non-commercial/commercial use rules.

## Why the key must not be placed into browser JavaScript

The GitHub Pages site is static. Any value embedded into client-side JS, HTML, JSON, or a public repository can be copied by any visitor. Therefore the connector uses:

- local environment variable with the Scopus key;
- GitHub Actions repository secret with the Scopus key;
- optional institutional token secret.

The static site publishes only the harvested JSON outputs, never the credential.

## Main endpoints used

Author profile:

`GET https://api.elsevier.com/content/author/author_id/{SCOPUS_AUTHOR_ID}?view=ENHANCED`

Fallback:

`GET https://api.elsevier.com/content/author/author_id/{SCOPUS_AUTHOR_ID}?view=STANDARD`

Publication list:

`GET https://api.elsevier.com/content/search/scopus?query=AU-ID({SCOPUS_AUTHOR_ID})&count=25&start=0&view=STANDARD`

Headers:

`X-ELS-APIKey: <secret>`

Optional:

`X-ELS-Insttoken: <secret>`

## Outputs

The connector writes:

- `data/scopus/scopus_author_<id>_raw.json`
- `data/scopus/scopus_author_<id>_works.json`
- `data/scopus/scopus_author_<id>_metrics.json`
- `data/scopus/scopus_author_<id>_access_report.json`

## Test command

```bash
export SCOPUS_API_KEY="..."
export SCOPUS_AUTHOR_ID="57211062810"
python scripts/harvest_scopus.py
```

## Commercial product implication

For a reusable scholar-site product, Scopus should be a configurable module. The client provides:

- Scopus Author ID;
- optionally an API key;
- optionally an Institutional Token.

If no key is available, the site should still work using eLibrary, ORCID, OpenAlex, Crossref, Google Scholar link, and manually verified Scopus metrics.
