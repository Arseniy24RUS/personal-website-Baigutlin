# GitHub Actions credentials

Set repository secrets under Settings → Secrets and variables → Actions:

| Secret | Purpose |
| --- | --- |
| `ELIBRARY_OPENVPN_CONFIG_B64` | Base64-encoded authorized home OpenVPN configuration |
| `ELIBRARY_USERNAME`, `ELIBRARY_PASSWORD` | Ordinary eLibrary login |
| `WOS_ORCID_USERNAME`, `WOS_ORCID_PASSWORD` | Ordinary ORCID login for WoS |
| `BROWSER_SESSION_KEY` | This repository's independent base64-encoded 32-byte encryption key |
| `SCOPUS_API_KEY` | Elsevier API key; access depends on entitlement |
| `SCOPUS_INST_TOKEN` | Optional institution entitlement |
| `WOS_STARTER_API_KEY`, `WOS_RESEARCHER_API_KEY` | Optional official Clarivate APIs |

Shared login credentials are permitted; profile identifiers in `config/profile.yml` remain Baigutlin's. Never reuse source-site observations or browser-session artifacts. Legacy cookie secrets are not needed. GitHub discovery uses the ordinary workflow token and requires none of these academic secrets.

After configuring access, run a dry refresh and inspect the sanitized source/component report. A validated retained dataset does not imply every upstream source was available. See [browser sessions](BROWSER_SESSIONS.md) and [operations](REFRESH_OPERATIONS.md).
