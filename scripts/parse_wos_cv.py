"""Parse the observed native Web of Science full-profile CV JSON export.

This is an adapter for the actual export shape, not the Researcher API or the
WOSNX search format. It performs no I/O and has no clock. The caller supplies the
time at which the authenticated, author-verified export was obtained. Historical
files must not be represented by their later import time.

Only academic allowlisted fields leave the parser. In particular, full CVs may
contain contact details, reviews and other personal sections: do not publish the
input document. Collection and persistence remain the caller's responsibility.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html
import re

from source_health import source_result


CORE_SCOPE = 'web_of_science_core_collection'
SOURCE = 'web_of_science_cv_export'
METHOD = 'cv_export_official_core_total'
CORE_FIELDS = ('publications', 'citations', 'h_index')
MAX_RECORDS = 100000
CV_REASONS = frozenset({
    'cv_schema_invalid', 'cv_identity_mismatch', 'cv_observed_at_invalid',
    'cv_metrics_incomplete', 'cv_metric_mismatch', 'cv_publications_incomplete',
    'cv_record_invalid', 'cv_conflicting_uid',
})


class CvParseError(ValueError):
    """Fixed diagnostic code; never echo values from a private CV."""

    def __init__(self, reason):
        self.reason = reason if reason in CV_REASONS else 'cv_schema_invalid'
        super().__init__(self.reason)


def _count(value):
    # bool, negative, null, strings and floats must not become observed counts.
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _text(value, limit=10000):
    if not isinstance(value, str) or len(value) > limit:
        return None
    value = html.unescape(re.sub(r'<[^>]*>', '', value))
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    return re.sub(r'\s+', ' ', value).strip() or None


def _doi(value):
    value = _text(value, 1000)
    if not value:
        return None
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value, flags=re.I).lower()
    # Do not carry an arbitrary URL or its query/session parameters into output.
    return value if re.fullmatch(r'10\.\d{4,9}/[^\s?#]+', value) else None


def _stamp(value):
    if not isinstance(value, str):
        raise CvParseError('cv_observed_at_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise CvParseError('cv_observed_at_invalid') from None


def _uid(value):
    return value if isinstance(value, str) and re.fullmatch(r'WOS:[A-Z0-9]{1,100}', value) else None


def _record(raw, stamp):
    uid = _uid(raw.get('ut'))
    title = _text(raw.get('title'))
    if uid is None or title is None:
        return None
    authors = raw.get('publication_authors')
    authors = authors if isinstance(authors, dict) else {}
    publication_date = _text(raw.get('publication_date'), 100)
    year = re.search(r'\b((?:18|19|20|21)\d{2})\b', publication_date or '')
    citations = _count(raw.get('citation_count'))
    return {
        'source': SOURCE, 'sources': ['wos'], 'wos_uid': uid,
        'title': title, 'venue': _text(raw.get('journal')),
        'doi': _doi(raw.get('doi')), 'authors_raw': _text(authors.get('authors')),
        'publication_date': publication_date, 'year': int(year[1]) if year else None,
        'url': 'https://www.webofscience.com/wos/woscc/full-record/' + uid,
        'wos_citations': citations, 'observed_at': stamp,
        'citation_observed_at': {'wos': stamp if citations is not None else None},
        'retained_citation_fields': [] if citations is not None else ['wos_citations'],
    }


def _state(stamp, *, complete, observed_count, count, reason):
    state = source_result(status='success' if complete else 'partial' if observed_count else 'error',
                          attempted_at=stamp, count=count, reason=None if complete else reason)
    state.update(scope=CORE_SCOPE, observed_count=observed_count)
    if observed_count or complete:
        state['last_observation_at'] = stamp
    return state


def parse_wos_cv(payload, *, observed_at, researcher_id='AAN-4717-2020'):
    """Return ``{'report': ..., 'payloads': {metrics, publications, details}}``.

    Identity or structural errors raise CvParseError. Usable component/row
    observations survive incomplete lists, missing counters and conflicting
    duplicates. This function neither merges a baseline nor writes a checkpoint.

    Completeness is based on total Core count == period Core count == distinct
    valid Core UID count, not localized date labels. Total aggregate fields are
    independent of the selected period; period counters never replace them.
    """
    stamp = _stamp(observed_at)
    if not isinstance(payload, dict):
        raise CvParseError('cv_schema_invalid')
    author = payload.get('author')
    if (not isinstance(researcher_id, str) or not re.fullmatch(r'[A-Z0-9]+-\d+-\d{4}', researcher_id)
            or not isinstance(author, dict) or author.get('rid') != researcher_id):
        raise CvParseError('cv_identity_mismatch')
    records = payload.get('records')
    publication = records.get('publication') if isinstance(records, dict) else None
    if not isinstance(publication, dict):
        raise CvParseError('cv_schema_invalid')
    raw_rows = publication.get('list')
    if not isinstance(raw_rows, list) or len(raw_rows) > MAX_RECORDS:
        raise CvParseError('cv_schema_invalid')

    period = payload.get('period')
    period = period if isinstance(period, dict) else {}
    context = {
        'date_generated': _text(payload.get('date_generated'), 100),
        'period': {key: _text(period.get(key), 100) for key in ('start', 'end')},
        'total_publications': _count(publication.get('total_publications')),
        'period_publications': _count(publication.get('period_publications')),
        'total_publications_cc': _count(publication.get('total_publications_cc')),
        'period_publications_cc': _count(publication.get('period_publications_cc')),
    }
    rows, conflicts, invalid_uids = {}, set(), set()
    invalid_count = excluded_count = duplicate_count = 0
    for raw in raw_rows:
        if not isinstance(raw, dict):
            invalid_count += 1
            continue
        raw_uid = raw.get('ut')
        if raw_uid in (None, '') or (isinstance(raw_uid, str) and not raw_uid.startswith('WOS:')):
            excluded_count += 1
            continue
        row = _record(raw, stamp)
        if row is None:
            invalid_count += 1
            if _uid(raw_uid):
                invalid_uids.add(raw_uid)
                if raw_uid in rows:
                    conflicts.add(raw_uid)
                rows.pop(raw_uid, None)
            continue
        uid = row['wos_uid']
        if uid in invalid_uids:
            conflicts.add(uid)
        if uid in conflicts:
            continue
        if uid in rows:
            duplicate_count += 1
            if rows[uid] != row:
                conflicts.add(uid)
                rows.pop(uid)
            continue
        rows[uid] = row

    total = context['total_publications_cc']
    period_total = context['period_publications_cc']
    coverage = total is not None and total == period_total == len(rows)
    list_reason = ('cv_conflicting_uid' if conflicts else 'cv_record_invalid' if invalid_count
                   else 'cv_publications_incomplete')
    list_complete = coverage and not conflicts and not invalid_count
    values = {
        'publications': total,
        'citations': _count(publication.get('total_citations')),
        'h_index': _count(publication.get('total_h_index')),
    }
    mismatches = set()
    if values['h_index'] is not None:
        if ((total is not None and values['h_index'] > total)
                or (values['citations'] is not None and values['h_index'] ** 2 > values['citations'])):
            mismatches.add('h_index')
    if total == 0:
        for key in ('citations', 'h_index'):
            if values[key] not in (None, 0):
                mismatches.add(key)
    if list_complete and all(row['wos_citations'] is not None for row in rows.values()):
        citations = sorted((row['wos_citations'] for row in rows.values()), reverse=True)
        h_index = sum(value >= position for position, value in enumerate(citations, 1))
        for key, calculated in (('citations', sum(citations)), ('h_index', h_index)):
            if values[key] is not None and values[key] != calculated:
                mismatches.add(key)
    if mismatches:
        for key in mismatches:
            values[key] = None
        list_complete = False
        list_reason = 'cv_metric_mismatch'
    known = {key: value for key, value in values.items() if value is not None}
    metrics_complete = len(known) == len(CORE_FIELDS)
    components = {
        'metrics': _state(stamp, complete=metrics_complete, observed_count=len(known),
                          count=1 if known else 0,
                          reason='cv_metric_mismatch' if mismatches else 'cv_metrics_incomplete'),
        'publications': _state(stamp, complete=list_complete, observed_count=len(rows),
                               count=len(rows), reason=list_reason),
    }
    components['publications'].update(core_record_count=len(rows), expected_core_count=total,
        period_core_count=period_total, excluded_non_core_count=excluded_count,
        invalid_record_count=invalid_count, duplicate_uid_count=duplicate_count,
        conflicting_uid_count=len(conflicts))
    metrics = {
        'source': SOURCE, 'researcher_id': researcher_id,
        'source_url': 'https://www.webofscience.com/wos/author/record/' + researcher_id,
        'summary': {**values, 'core_collection_publications': values['publications']},
        'metric_methods': {key: METHOD for key in known},
        'metric_observed_at': {key: stamp if key in known else None for key in CORE_FIELDS},
        'retained_metric_fields': [key for key in CORE_FIELDS if key not in known],
        'export_context': context,
    }
    if known:
        metrics['generated_at'] = stamp
    if metrics_complete:
        metrics['last_success_at'] = stamp
    complete = metrics_complete and list_complete
    successful = bool(known) or bool(rows) or list_complete
    reason = next((state['reason'] for state in components.values() if state.get('reason')), None)
    report = source_result(status='success' if complete else 'partial' if successful else 'error',
                           count=len(rows), reason=reason, attempted_at=stamp)
    report.update(components=components, researcher_id=researcher_id, transport='browser_cv_export',
                  identity_verified=True, export_context=context)
    return {'report': report, 'payloads': {'metrics': metrics, 'publications': list(rows.values()), 'details': {}}}
