"""Public diagnostics must never contain authentication or network secrets."""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

SECRET_KEYS = re.compile(r'^(authorization|proxy.authorization|cookie|set.cookie|password|passwd|access.token|refresh.token|id.token|api.key|session.id|sid|token|srcappsid|hmac|signature|sig|jsessionid|auth.code|wos_input_storage_state)$', re.I)
PRIVATE_KEYS = re.compile(r'^(ip|ip_address|public_ip|remote_ip|client_ip|local_address|remote_address|headers|request_headers|response_headers|cookies|storage_state)$', re.I)
SECRET_QUERY = re.compile(r'^(sid|srcappsid|hmac|signature|sig|jsessionid|sessionid|session_id|token|access_token|refresh_token|id_token|api_key|apikey|password|authorization|auth_token|authcode)$', re.I)


def canonical_wos_url(value: str) -> str:
    """Publication/profile identities never need a signed browser session URL."""
    try:
        u = urlsplit(value)
        if (u.hostname or '').lower() not in {'webofscience.com', 'www.webofscience.com'}:
            return value
        path = unquote(u.path)
        record = re.fullmatch(r'/wos/[^/]+/full-record/(WOS:[A-Za-z0-9]+)', path, re.I)
        if record:
            return 'https://www.webofscience.com/wos/woscc/full-record/' + record.group(1)
        author = re.fullmatch(r'/wos/author/record/([A-Za-z0-9-]+)', path)
        if author:
            return 'https://www.webofscience.com/wos/author/record/' + author.group(1)
        return value
    except ValueError:
        return value

def clean_url(value: str) -> str:
    try:
        value = canonical_wos_url(value)
        u = urlsplit(value)
        if not u.scheme or not u.netloc:
            return value
        params = parse_qsl(u.query, keep_blank_values=True)
        fragments = parse_qsl(u.fragment, keep_blank_values=True) if '=' in u.fragment else []
        oauth = (u.hostname or '').endswith(('orcid.org', 'clarivate.com', 'webofscience.com'))
        sensitive = lambda key: bool(SECRET_QUERY.fullmatch(key)) or (oauth and key.lower() == 'code')
        if not u.username and not any(sensitive(k) for k, _ in params + fragments):
            return value
        host = u.hostname or ''
        port = f':{u.port}' if u.port else ''
        query = urlencode([(k, '[REDACTED]' if sensitive(k) else v) for k, v in params])
        fragment = urlencode([(k, '[REDACTED]' if sensitive(k) else v) for k, v in fragments]) if fragments else u.fragment
        return urlunsplit((u.scheme, host + port, u.path, query, fragment))
    except ValueError:
        return '[invalid URL]'

def sanitize(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if SECRET_KEYS.search(k) or PRIVATE_KEYS.search(k) else sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        text = re.sub(r'https?://[^\s<>"\']+', lambda m: clean_url(m.group()), value)
        text = re.sub(r'(?i)(\b(?:SID|SrcAppSID|HMAC|signature|jsessionid|access_token|refresh_token|id_token|api_key|password)=)[^\s&;]+', r'\1[REDACTED]', text)
        for key, secret in os.environ.items():
            if len(secret) >= 6 and re.search(r'(PASSWORD|COOKIE|TOKEN|API_KEY|STORAGE_STATE|OPENVPN_CONFIG)', key):
                text = text.replace(secret, '[REDACTED]')
        return text
    return value

def sanitize_public_tree(root: Path) -> int:
    changed = 0
    for path in (root / 'data').rglob('*.json'):
        original = json.loads(path.read_text(encoding='utf-8-sig'))
        cleaned = sanitize(original)
        if cleaned != original:
            path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            changed += 1
    return changed

if __name__ == '__main__':
    print(f'Sanitized {sanitize_public_tree(Path.cwd())} public JSON files.')
