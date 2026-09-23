# Browser sessions

The target is Baigutlin's profile; the authorized eLibrary and ORCID login accounts may belong to the site maintainer. Account identity and researcher identity are separate. Collection must verify the target author before accepting publications or metrics.

Sessions are stored only in encrypted Actions artifacts (`browser-session-v1-elibrary`, `browser-session-v1-wos`). Envelopes bind the repository, provider and target identifier. This repository uses its own `BROWSER_SESSION_KEY`: base64 of 32 random bytes. Source-site sessions and snapshots must never be imported as Baigutlin observations.

Weekly refresh is Monday 05:47 UTC (08:47 Moscow); other days at the same time only maintain sessions. This is offset from the source site's 03:17 UTC schedule. A shared VPN can still be busy during manual runs.

Use **Refresh scientist portfolio data**, `maintain_session=true`, for a manual check. Ordinary credential login is attempted when no verified session is available. `wos_auth_mode=fresh_orcid` tests ordinary ORCID login independently. Interactive verification, account linking or unavailable providers produce diagnostics and preserve previous data; they are not reported as fresh successful observations.

Raw browser state, cookies, passwords and authenticated HTML are not published. The workflow uploads only encrypted sessions and sanitized diagnostic metadata. `BROWSER_SESSION_PREVIOUS_KEY` supports key rotation; optional encrypted `session_bootstrap` is repository and target specific. See [operations](REFRESH_OPERATIONS.md).
