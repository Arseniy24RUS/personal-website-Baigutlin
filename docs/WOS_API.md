# Web of Science API access

The WoS collection path supports Clarivate's official HTTPS APIs. These use an
application API key rather than an interactive WoS/ORCID browser session.
Configuring a key does not itself establish access: accept the integration only
after two independent Actions runs return complete live results for the intended
author and collection, without a new browser bootstrap.

## Available plans

The [Starter API](https://developer.clarivate.com/apis/wos-starter) offers:

| Plan | Eligibility | Data and limit |
| --- | --- | --- |
| Free Trial | Anyone, including without a WoS subscription | Bibliography; no times-cited counts; 50 requests/day, 1/second |
| Free Institutional Member | Members of an organization subscribing to WoS | Bibliography and document citations; 5,000 requests/day |

The [Researcher API](https://developer.clarivate.com/apis/wos-researcher) supplies
researcher-level metadata and requires an additional paid licence. Its public
page does not quote a fixed price. No paid subscription is purchased by this
repository. The Starter Trial page does not establish indefinite availability.

Starter citation data can support calculated publication, citation and h-index
values only after a complete Core Collection result with known citation counts
for every work. Trial responses cannot supply those citation metrics. A missing
field is unknown, not zero. The existing verified site metrics remain available
when an API entitlement does not provide a complete replacement.

## Application registration

The [Developer Portal](https://developer.clarivate.com/) requires an application
and a subscription for that application. Its current sign-in form accepts an
existing Clarivate email/password; the observed form has no ORCID button. A WoS
session obtained through ORCID is not proof of a Developer Portal account.

Prepared application details:

| Field | Value |
| --- | --- |
| Application ID | `baigutlin-personal-website` (subject to availability) |
| Name | Baigutlin academic portfolio |
| Website | `https://baigutlin.ru/` |
| Execution | Private GitHub Actions job; key is never sent to the public website |
| Purpose | Weekly retrieval of the owner's publications and bibliometric indicators for ResearcherID AAN-4717-2020 |
| Initial plan | Starter Free Trial; use Institutional Member only with verified eligibility |

Portal registration or API subscription may require accepting Clarivate terms
and approval by Clarivate. Prepare the application before the owner reviews that
final action; do not infer institutional entitlement from a home IP or an ORCID
login. See the [application instructions](https://developer.clarivate.com/help/application)
and [portal FAQ](https://developer.clarivate.com/content/developer-portal-faq).

Save approved credentials as repository Actions secrets:

- `WOS_STARTER_API_KEY` for the Starter subscription.
- `WOS_RESEARCHER_API_KEY` for a separately entitled Researcher subscription.

Keys belong in the `X-ApiKey` HTTP header. Do not place them in workflow inputs,
URLs, public JSON, source code, screenshots or diagnostic artifacts. Existing
WoS browser credentials and encrypted state remain the fallback configuration.

## Author and collection

The configured target is ResearcherID `AAN-4717-2020`. Starter requests the
explicit Core Collection database:

```text
GET /apis/wos-starter/v2/documents?db=WOS&q=AI%3DAAN-4717-2020&limit=50&page=1
```

`AI` is the documented author identifier search field. Researcher queries the
specific researcher and paginated documents, excluding non-indexed documents.
An indexed-document total is not automatically a Core Collection total. In the
verified browser capture there were 13 Core works, 19 indexed works and 28 works
overall. The collector must preserve these different scopes.

The official response contracts are the [Starter specification](https://developer.clarivate.com/apis/wos-starter/swagger)
and [Researcher specification](https://developer.clarivate.com/apis/wos-researcher/swagger).
Public publication links are reconstructed from validated WoS UIDs, not copied
from signed API response links.

## Failure handling and acceptance

The API path uses the same isolated staging area, component checkpoints and
retention guard as the browser path. Metrics and publication observations are
independent. An API success survives a later browser failure. A complete API
result avoids starting the WoS browser; a partial result can be enriched through
the existing authenticated browser collector. Without API secrets, collection
continues through the browser as before.

Pagination must finish with stable totals and distinct records before metrics
are calculated. Partial lists add verified works without removing published
ones. Repeated responses and concurrent publication merge by WoS UID and
normalized DOI while retaining existing public IDs and manual fields.

Full API-only acceptance requires citation-capable access. Starter Trial can
verify automatic bibliography collection, but its metric component remains
incomplete unless the browser supplies the missing data. A successful Trial
list must not be presented as automatic metric collection.

After obtaining a citation-capable key:

1. Dispatch the main refresh workflow with `sources=wos`, `dry_run=true` and no
   session bootstrap. Verify live component statuses, author, collection,
   distinct UID count, pagination and citation coverage.
2. Repeat on an independent runner. Confirm the same API route succeeds without
   opening WoS or using browser cookies.
3. Dispatch `dry_run=false`, wait for Pages for the data commit, and verify the
   published values, previous IDs, images and visitor-facing pages.

The weekly schedule remains Monday 08:47 Moscow. Browser maintenance continues
on the other days; it does not refresh scientific data timestamps. API expiry,
quota or entitlement failures remain visible in Actions while the last verified
site data are retained. Do not describe implemented API support as a successful
live API collection until these checks have actually passed.
