#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import csv
import copy
import difflib
import json
import re
from source_health import component_state, is_verified, observation_time, load_checkpoint, materialize_checkpoint
from report_safety import clean_url
from parse_wos_author_profile import normalized_summary as normalized_wos_summary
from export_profile_env import public_profile_identifiers
from harvest_open_sources import work_title_key, same_open_work, publication_type

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

DATA = Path('data')
PUBLIC = DATA / 'public'
PUBLIC.mkdir(parents=True, exist_ok=True)
MIN_ELIBRARY_RECORDS = 50


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def profile():
    if yaml and Path('config/profile.yml').exists():
        return (yaml.safe_load(Path('config/profile.yml').read_text(encoding='utf-8')) or {}).get('profile', {})
    raise ValueError('Profile configuration/PyYAML unavailable; refusing to rebuild public data')


def first_number(*values):
    for value in values:
        number = as_number(value)
        if number is not None:
            return number
    return None


def is_fresh(health):
    return is_verified(health)


def normalize_health(report, previous=None, record_count=0):
    report = report if isinstance(report, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    origin = report.get('origin') or ('snapshot' if any(x in str(report.get('used_source', '')) for x in ('snapshot', 'previous')) else 'snapshot')
    status = report.get('status')
    if status not in {'success', 'partial', 'blocked', 'error'}:
        status = 'blocked' if report.get('human_verification_required') or report.get('status') == 'not_ready' else 'partial'
    attempted = report.get('attempted_at') or report.get('generated_at')
    successful = report.get('last_success_at') or previous.get('last_success_at')
    if not successful:
        match = re.search(r'_(\d{8})T(\d{6})Z', str(report.get('snapshot_path') or ''))
        if match:
            successful = datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat()
    result = {'status': status, 'attempted_at': attempted, 'last_success_at': successful,
            'origin': origin, 'complete': report.get('complete') is True,
            'record_count': report.get('record_count', record_count),
            'reason': report.get('reason') or ('legacy_report_without_verified_freshness' if status != 'success' else None)}
    for key in ('last_observation_at', 'observed_count', 'expected_count', 'pending', 'fetched', 'failed', 'stage'):
        if key in report:
            result[key] = report[key]
    components = report.get('components')
    if isinstance(components, dict):
        names = set(components) | set(previous.get('components') or {})
        result['components'] = {name: normalize_health(components.get(name), component_state(previous, name))
                                for name in names}
    return result


def citation_is_new(record, target, provider, fresh=False):
    field = {'wos': 'wos_citations', 'elibrary': 'rinc_citations', 'scopus': 'scopus'}.get(provider)
    explicit = record.get('citation_observed_at') or {}
    if provider in explicit:
        return observation_time(explicit[provider]) > observation_time((target.get('citation_observed_at') or {}).get(provider))
    if field in (record.get('retained_citation_fields') or []):
        return False
    observed = citation_observation(record, provider)
    if observed:
        return observation_time(observed) > observation_time((target.get('citation_observed_at') or {}).get(provider))
    return fresh


def citation_observation(record, provider):
    # A fresh metadata response does not renew an older citation observation.
    explicit = record.get('citation_observed_at') or {}
    if provider in explicit:
        return explicit[provider]
    field = {'wos': 'wos_citations', 'elibrary': 'rinc_citations', 'scopus': 'scopus'}.get(provider)
    return None if field in (record.get('retained_citation_fields') or []) else record.get('observed_at')


def mark_citation_observation(target, record, provider):
    field = {'wos': 'wos_citations', 'elibrary': 'rinc_citations', 'scopus': 'scopus'}.get(provider)
    if field in (record.get('retained_citation_fields') or []) and provider not in (record.get('citation_observed_at') or {}):
        return
    observed = citation_observation(record, provider)
    if observation_time(observed) > float('-inf'):
        target.setdefault('citation_observed_at', {})[provider] = observed


def load_source_health(ids, previous=None):
    previous = previous or {}
    sid = ids.get('scopus_author_id', '')
    open_report = read_json(DATA / 'open/harvest_report.json', {})
    reports = {
        'elibrary': read_json(DATA / 'elibrary/browser_fetch_report.json', {}),
        'wos': read_json(DATA / 'wos/harvest_report.json', {}),
        'scopus': read_json(DATA / f'scopus/scopus_author_{sid}_access_report.json', {}),
        'media': read_json(DATA / 'media/harvest_report.json', {}),
    }
    for name, key in [('orcid', 'orcid'), ('openalex', 'openalex_works'), ('crossref', 'crossref')]:
        reports[name] = {'generated_at': open_report.get('generated_at'), **((open_report.get('providers') or {}).get(key) or {})}
    if not reports['elibrary'].get('last_success_at') and not (previous.get('elibrary') or {}).get('last_success_at'):
        fallback = normalize_health(read_json(DATA / 'elibrary/items_fetch_report.json', {}))
        reports['elibrary']['last_success_at'] = fallback.get('last_success_at')
    return {name: normalize_health(report, previous.get(name)) for name, report in reports.items()}


def recover_source_checkpoints():
    for provider, filename in [('elibrary', 'browser_fetch_report.json'), ('wos', 'harvest_report.json')]:
        path = DATA / provider / 'collection_checkpoint.json'
        checkpoint = load_checkpoint(path)
        legacy = read_json(DATA / provider / filename, {})
        if checkpoint and observation_time(checkpoint['report'].get('attempted_at')) >= observation_time(legacy.get('attempted_at')):
            materialize_checkpoint(path, provider)


def clean(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def nt(s):
    return re.sub(r'[^a-zа-я0-9]+', ' ', clean(s).lower().replace('ё', 'е')).strip()


def nd(doi):
    if not doi:
        return None
    return re.sub(r'^https?://(dx\.)?doi\.org/', '', clean(doi).lower()).rstrip('.,;') or None


def source_identity_aliases(row):
    """Index every strong identifier, including former WoS UIDs and DOI forms."""
    aliases = set()
    for field in ('elibrary_item_id', 'eid', 'id'):
        if row.get(field):
            aliases.add((field, clean(row[field]).lower()))
    for item in [row, *(row.get('wos_records') or [])]:
        if not isinstance(item, dict):
            continue
        for field, normalizer in (('wos_uid', lambda value: clean(value).upper()), ('doi', nd)):
            values = [item.get(field), *(item.get(field + '_aliases') or [])]
            for value in values:
                normalized = normalizer(value)
                if normalized:
                    aliases.add((field, normalized))
    return aliases


def index_source_aliases(index, row, position):
    for alias in source_identity_aliases(row):
        index.setdefault(alias, set()).add(position)


def match_source_aliases(index, row):
    """An exact UID may narrow a shared DOI; contradictory bridges stay separate."""
    matches = [index[alias] for alias in source_identity_aliases(row) if alias in index]
    if not matches:
        return None, False
    candidates = set.intersection(*matches)
    if len(candidates) == 1:
        return next(iter(candidates)), False
    # Existing separately published records are never collapsed to resolve a tie.
    return None, True


def remember_source_aliases(target, incoming):
    aliases = source_identity_aliases(target) | source_identity_aliases(incoming)
    for field, normalizer in (('wos_uid', lambda value: clean(value).upper()), ('doi', nd)):
        primary = normalizer(target.get(field))
        extra = sorted(value for kind, value in aliases if kind == field and value != primary)
        if extra:
            target[field + '_aliases'] = extra


def conflicting_source_identity(previous, incoming):
    old, new = source_identity_aliases(previous), source_identity_aliases(incoming)
    for field in ('wos_uid', 'doi'):
        known = {value for kind, value in old if kind == field}
        observed = {value for kind, value in new if kind == field}
        if known and observed and not known & observed:
            return True
    return False


def has_cyrillic(s):
    return bool(re.search(r'[А-Яа-яЁё]', str(s or '')))


def has_latin(s):
    return bool(re.search(r'[A-Za-z]', str(s or '')))


def as_number(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict):
        return as_number(v.get('value') if v.get('value') is not None else v.get('raw'))
    if v is None:
        return None
    m = re.search(r'-?\d+(?:\.\d+)?', str(v).replace('\xa0', ' ').replace(',', '.'))
    if not m:
        return None
    num = float(m.group(0))
    return int(num) if num.is_integer() else num


def metric_value(mapping, *labels):
    if not isinstance(mapping, dict):
        return None
    for label in labels:
        if label in mapping:
            v = as_number(mapping[label])
            if v is not None:
                return v
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for label in labels:
        key = str(label).lower()
        for k, v in lowered.items():
            if key in k:
                n = as_number(v)
                if n is not None:
                    return n
    return None


def usable_source_pages(value) -> bool:
    text = clean(value)
    # Unknown pagination is not a page range. Keep existing editorial values;
    # validate only values being newly added from a provider.
    return bool(re.search(r'\d', text) or re.fullmatch(r'[ivxlcdm]+(?:\s*[-–—]\s*[ivxlcdm]+)?', text, re.I))


def set_missing(p: dict, key: str, value) -> bool:
    if value in (None, '', []):
        return False
    if key in ('pages', 'page') and not usable_source_pages(value):
        return False
    if p.get(key) in (None, '', []):
        p[key] = value
        return True
    return False


def set_lang_field(p, base, value, prefer=None):
    value = clean(value)
    if not value:
        return False
    lang = prefer
    if lang not in ('ru', 'en'):
        lang = 'ru' if has_cyrillic(value) else 'en' if has_latin(value) else None
    changed = False
    if lang:
        changed = set_missing(p, f'{base}_{lang}', value) or changed
    changed = set_missing(p, base, value) or changed
    return changed


def addsrc(p, s):
    sources = p.get('sources') or []
    if isinstance(sources, str):
        sources = [x.strip() for x in sources.split(',') if x.strip()]
    if not sources:
        sources = ['elibrary'] if p.get('elibrary_item_id') else []
    if s and s not in sources:
        sources.append(s)
    p['sources'] = sources


def enrich_localized_fields(p):
    set_lang_field(p, 'title', p.get('title'))
    set_lang_field(p, 'venue', p.get('venue'))
    sc = p.get('scopus') or {}
    if sc.get('title'):
        set_lang_field(p, 'title', sc.get('title'), 'en')
    if sc.get('journal_or_source') or sc.get('source_title'):
        set_lang_field(p, 'venue', sc.get('journal_or_source') or sc.get('source_title'), 'en')
    for r in p.get('open_sources') or []:
        if r.get('title'):
            set_lang_field(p, 'title', r.get('title'))
        if r.get('venue'):
            set_lang_field(p, 'venue', r.get('venue'))
    for r in p.get('wos_records') or []:
        if r.get('title_en') or r.get('title'):
            set_lang_field(p, 'title', r.get('title_en') or r.get('title'), 'en')
        if r.get('venue_en') or r.get('venue'):
            set_lang_field(p, 'venue', r.get('venue_en') or r.get('venue'), 'en')
    p.setdefault('title_ru', p.get('title'))
    p.setdefault('venue_ru', p.get('venue'))


def elib_key(p):
    if p.get('elibrary_item_id'):
        return ('elibrary', str(p.get('elibrary_item_id')))
    doi = nd(p.get('doi'))
    if doi:
        return ('doi', doi)
    return ('title_year', nt(p.get('title') or p.get('title_ru') or p.get('title_en')), str(p.get('year') or ''))


def same_open_observation(previous, incoming):
    if previous.get('source') != incoming.get('source'):
        return False
    for field in ('put_code', 'openalex_id'):
        if previous.get(field) and incoming.get(field):
            return str(previous[field]) == str(incoming[field])
    if previous.get('doi') and incoming.get('doi'):
        return nd(previous['doi']) == nd(incoming['doi'])
    return bool(nt(previous.get('title'))) and (
        nt(previous.get('title')), str(previous.get('year') or '')
    ) == (nt(incoming.get('title')), str(incoming.get('year') or ''))


def merge_open_observation(target, incoming, prefer_incoming=True):
    """One current observation per provider/work; retain old published snapshots.

    Raw fields, metrics, and additional DOI provenance can change independently
    of work identity. Historical duplicates are kept, but new versions enrich
    the last matching observation rather than append another identical work.
    Concurrent publication merging keeps the destination's existing values.
    """
    observations = target.setdefault('open_sources', [])
    matched = next((row for row in reversed(observations) if same_open_observation(row, incoming)), None)
    if matched is None:
        observations.append(copy.deepcopy(incoming))
        return
    for field, value in incoming.items():
        if value in (None, '', [], {}):
            continue
        previous = matched.get(field)
        if field == 'sources' and isinstance(value, list):
            matched[field] = list(dict.fromkeys((previous or []) + value))
        elif isinstance(previous, dict) and isinstance(value, dict):
            matched[field] = ({**previous, **copy.deepcopy(value)} if prefer_incoming
                              else {**copy.deepcopy(value), **previous})
        elif prefer_incoming or previous in (None, '', [], {}):
            matched[field] = copy.deepcopy(value)


def merge_publication_sets(*datasets):
    # The first dataset is the published baseline. Preserve every row, including
    # intentional duplicates; later sources may enrich it, never coalesce it away.
    rows = copy.deepcopy(list(datasets[0] or [])) if datasets else []
    by_key = {}
    aliases = {}
    for position, row in enumerate(rows):
        by_key.setdefault(elib_key(row), []).append(row)
        index_source_aliases(aliases, row, position)
    for dataset in datasets[1:]:
        for original in dataset or []:
            if not isinstance(original, dict) or not (original.get('title') or original.get('title_ru') or original.get('title_en') or original.get('elibrary_item_id')):
                continue
            incoming = copy.deepcopy(original)
            for field in ('pages', 'page'):
                if incoming.get(field) and not usable_source_pages(incoming[field]):
                    incoming.pop(field)
            key = elib_key(incoming)
            targets = by_key.get(key, [])
            if any(kind == 'wos_uid' for kind, _ in source_identity_aliases(incoming)):
                position, ambiguous = match_source_aliases(aliases, incoming)
                if ambiguous:
                    continue
                if position is not None:
                    targets = [rows[position]]
                else:
                    targets = [target for target in targets if not conflicting_source_identity(target, incoming)]
            if not targets:
                rows.append(incoming)
                by_key[key] = [incoming]
                index_source_aliases(aliases, incoming, len(rows) - 1)
                continue
            for target in targets:
                remember_source_aliases(target, incoming)
                for name, value in incoming.items():
                    if name == 'open_sources':
                        for observation in value or []:
                            merge_open_observation(target, observation, prefer_incoming=False)
                    elif name in {'sources', 'wos_records'}:
                        existing = target.setdefault(name, [])
                        if isinstance(existing, str):
                            existing = target[name] = [part.strip() for part in existing.split(',') if part.strip()]
                        values = [value] if isinstance(value, str) else value or []
                        for item in values:
                            if item not in existing:
                                existing.append(copy.deepcopy(item))
                    else:
                        set_missing(target, name, value)
                position = next(i for i, row in enumerate(rows) if row is target)
                index_source_aliases(aliases, target, position)
    for row in rows:
        enrich_localized_fields(row)
    rows.sort(key=lambda row: (-(int(row.get('year') or 0) if str(row.get('year') or '').isdigit() else 0), int(row.get('number') or 999999)))
    return rows


def load_elib_tsv():
    rows = []
    t = DATA / 'elibrary/publications.tsv'
    if not t.exists():
        return rows
    for r in csv.reader(t.open(encoding='utf-8'), delimiter='\t'):
        if len(r) < 8:
            continue
        m = re.search(r'id=(\d+)', r[7] or '')
        rec = {
            'source': 'elibrary_rinc_tsv',
            'number': int(r[0]) if r[0].isdigit() else None,
            'elibrary_item_id': m.group(1) if m else None,
            'year': int(r[1]) if r[1].isdigit() else None,
            'rinc_citations': int(r[2]) if r[2].isdigit() else None,
            'title': r[3],
            'title_ru': r[3],
            'authors_raw': r[4],
            'venue': r[5] or None,
            'venue_ru': r[5] or None,
            'pages': r[6] or None,
            'doi': None,
            'url': r[7] or None,
            'sources': ['elibrary'],
        }
        enrich_localized_fields(rec)
        rows.append(rec)
    return rows


def profile_matches_existing(current_ids: dict) -> bool:
    existing_profile = read_json(DATA / 'public/profile.json', {})
    existing_ids = public_profile_identifiers(existing_profile) if isinstance(existing_profile, dict) else {}
    if not isinstance(existing_ids, dict):
        return False
    compared = 0
    matched = 0
    for key in ['elibrary_authorid', 'orcid', 'scopus_author_id', 'wos_researcher_id']:
        current = clean(current_ids.get(key))
        existing = clean(existing_ids.get(key))
        if existing and not current:
            return False
        if current and existing:
            compared += 1
            if current == existing:
                matched += 1
            else:
                return False
    return compared == 0 or matched > 0


def load_existing_publications_for_profile(ids):
    if not profile_matches_existing(ids):
        raise ValueError('Published profile identifiers differ; explicit migration required')
    path = DATA / 'public/publications.json'
    rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Published bibliography is invalid; refusing to overwrite it')
    previous = read_json(DATA / 'public/profile.json', {})
    for row in rows:
        # The original site used these field names. Add canonical aliases while
        # retaining every existing field and its curated bibliography verbatim.
        for legacy, canonical in [('authors', 'authors_raw'), ('citations_risc', 'rinc_citations')]:
            set_missing(row, canonical, row.get(legacy))
        if 'authors' in row or 'citations_risc' in row:
            set_missing(row, 'metadata_raw', row.get('source'))
        if not row.get('elibrary_item_id'):
            match = re.search(r'https?://(?:www\.)?elibrary\.ru/item\.asp\?id=(\d+)', row.get('url') or '')
            if match:
                row['elibrary_item_id'] = match.group(1)
        for provider, field in [('elibrary', 'rinc_citations'), ('wos', 'wos_citations'), ('scopus', 'scopus')]:
            state = component_state((previous.get('source_health') or {}).get(provider), 'publications')
            if row.get(field) is not None and state.get('last_success_at'):
                row.setdefault('citation_observed_at', {}).setdefault(provider, state['last_success_at'])
    return rows


def load_elib(ids):
    processed = read_json(DATA / 'processed/elibrary_publications.json', [])
    if not isinstance(processed, list):
        processed = []
    for p in processed:
        p.setdefault('sources', ['elibrary'])
        enrich_localized_fields(p)
    tsv = load_elib_tsv()
    public_existing = load_existing_publications_for_profile(ids)
    merged = merge_publication_sets(public_existing, tsv, processed)
    health = normalize_health(read_json(DATA / 'elibrary/browser_fetch_report.json', {}))
    state = component_state(health, 'publications')
    observations = {str(row.get('elibrary_item_id')): row for row in processed if row.get('elibrary_item_id')}
    for row in merged:
        observation = observations.get(str(row.get('elibrary_item_id')), {})
        if observation.get('rinc_citations') is not None and citation_is_new(observation, row, 'elibrary', is_fresh(state)):
            row['rinc_citations'] = observation['rinc_citations']
            mark_citation_observation(row, observation, 'elibrary')
    best_count = max(len(processed), len(tsv), len([p for p in public_existing if 'elibrary' in ','.join(p.get('sources', []) if isinstance(p.get('sources'), list) else [str(p.get('sources') or '')]) or p.get('elibrary_item_id')]), len(merged))
    if best_count >= MIN_ELIBRARY_RECORDS and len(merged) < MIN_ELIBRARY_RECORDS:
        candidates = [x for x in [tsv, public_existing, processed] if len(x) >= MIN_ELIBRARY_RECORDS]
        return max(candidates, key=len) if candidates else merged
    return merged


def indexes(records):
    return (
        {str(p.get('elibrary_item_id')): p for p in records if p.get('elibrary_item_id')},
        {nt(p.get('title')): p for p in records if p.get('title')},
        {(nt(p.get('title')), str(p.get('year') or '')): p for p in records if p.get('title')},
        {nd(p.get('doi')): p for p in records if nd(p.get('doi'))},
    )


def merge_scopus(canon, works, fresh=False):
    curated = read_json(DATA / 'curation/scopus_elibrary_map.json', {})
    by_item, by_title, by_ty, by_doi = indexes(canon)
    added = 0
    for w in works or []:
        eid = w.get('eid')
        doi = nd(w.get('doi'))
        target = None
        if eid in curated:
            target = by_item.get(str(curated[eid].get('elibrary_item_id')))
        if target is None and doi:
            target = by_doi.get(doi)
        if target is None:
            target = by_title.get(nt(w.get('title')))
        if target is None:
            target = by_ty.get((nt(w.get('title')), str(w.get('year') or w.get('cover_date') or '')[:4]))
        if target:
            addsrc(target, 'scopus')
            if citation_is_new(w, target, 'scopus', fresh) or not target.get('scopus'):
                target['scopus'] = copy.deepcopy(w)
                mark_citation_observation(target, w, 'scopus')
            if doi and not target.get('doi'):
                target['doi'] = doi
            set_lang_field(target, 'title', w.get('title'), 'en')
            set_lang_field(target, 'venue', w.get('journal_or_source') or w.get('source_title'), 'en')
        else:
            rec = {
                'source': 'scopus_api_auto',
                'number': None,
                'elibrary_item_id': None,
                'year': int(str(w.get('year') or w.get('cover_date') or '')[:4]) if str(w.get('year') or w.get('cover_date') or '')[:4].isdigit() else None,
                'rinc_citations': None,
                'title': w.get('title'),
                'authors_raw': w.get('creator') or '',
                'venue': w.get('journal_or_source') or w.get('source_title'),
                'pages': None,
                'doi': doi,
                'url': w.get('url') or (f"https://www.scopus.com/record/display.uri?eid={eid}" if eid else None),
                'sources': ['scopus'],
                'scopus': w,
                'auto_accept_reason': 'author-scoped Scopus AU-ID record',
            }
            enrich_localized_fields(rec)
            canon.append(rec)
            added += 1
            if doi:
                by_doi[doi] = rec
            title = nt(rec.get('title'))
            if title:
                by_title[title] = rec
                by_ty[(title, str(rec.get('year') or ''))] = rec
    return added


def merge_open(canon, records):
    curated = read_json(DATA / 'curation/open_elibrary_map.json', {})
    by_item, by_title, by_ty, by_doi = indexes(canon)
    enriched = added = 0
    pending = []
    for r in sorted(records or [], key=lambda row: (publication_type(row) == 'preprint', not bool(row.get('doi')))):
        doi = nd(r.get('doi'))
        title = nt(r.get('title'))
        target = None
        if doi in curated:
            target = by_item.get(str(curated[doi].get('elibrary_item_id')))
        if target is None and ('title:' + title) in curated:
            target = by_item.get(str(curated['title:' + title].get('elibrary_item_id')))
        if target is None and doi:
            target = by_doi.get(doi)
        if target is None:
            candidates = [row for row in canon if same_open_work(row, r)]
            # Never guess between already-published duplicate records.
            if len(candidates) == 1:
                target = candidates[0]
            elif len(candidates) > 1:
                pending.append({'reason': 'ambiguous_existing_identity', 'record': r})
                continue
        if target is None and not doi and publication_type(r) == 'preprint':
            possible = [row for row in canon if publication_type(row) == 'preprint' and row.get('doi')
                        and difflib.SequenceMatcher(None, work_title_key(row.get('title')), work_title_key(r.get('title'))).ratio() >= .90]
            if possible:
                pending.append({'reason': 'possible_preprint_title_variant', 'record': r,
                                'candidate_dois': [row['doi'] for row in possible]})
                continue
        src = r.get('source') or 'open_api'
        if target:
            for provider in list(r.get('sources') or []) + [src]:
                addsrc(target, provider)
            merge_open_observation(target, r)
            if doi and not target.get('doi'):
                target['doi'] = doi
            if r.get('venue') and not target.get('venue'):
                target['venue'] = r.get('venue')
            for field in ('authors_raw', 'volume', 'issue', 'pages', 'publisher'):
                set_missing(target, field, r.get(field))
            set_missing(target, 'publication_type', publication_type(r))
            set_lang_field(target, 'title', r.get('title'))
            set_lang_field(target, 'venue', r.get('venue'))
            enriched += 1
        else:
            if not r.get('authors_raw'):
                pending.append({'reason': 'authors_not_provided_by_sources', 'record': r})
                continue
            rec = {'source': src + '_auto', 'number': None, 'elibrary_item_id': None, 'year': int(r.get('year')) if str(r.get('year') or '').isdigit() else None, 'rinc_citations': None, 'title': r.get('title'), 'authors_raw': r.get('authors_raw') or '', 'venue': r.get('venue'), 'pages': r.get('pages'), 'volume': r.get('volume'), 'issue': r.get('issue'), 'publisher': r.get('publisher'), 'publication_type': publication_type(r), 'doi': doi, 'url': r.get('url') or r.get('landing_page_url'), 'sources': [src], 'open_sources': [r], 'auto_accept_reason': 'author-scoped ORCID/OpenAlex/Crossref record'}
            for provider in r.get('sources') or []:
                addsrc(rec, provider)
            enrich_localized_fields(rec)
            canon.append(rec)
            added += 1
            if doi:
                by_doi[doi] = rec
            if title:
                by_title[title] = rec
                by_ty[(title, str(rec.get('year') or ''))] = rec
    pending_path = DATA / 'open/pending_publications.json'
    if pending or pending_path.exists():
        write_json(pending_path, {'schema': 'open-publication-review/v1', 'records': pending})
    return enriched, added


def append_unique_wos_record(pub, record):
    existing = pub.setdefault('wos_records', [])
    uid = clean(record.get('wos_uid'))
    doi = nd(record.get('doi'))
    for item in existing:
        if uid and clean(item.get('wos_uid')) == uid:
            item.update({k: v for k, v in record.items() if v not in (None, '', [])})
            return
        if doi and nd(item.get('doi')) == doi:
            item.update({k: v for k, v in record.items() if v not in (None, '', [])})
            return
    existing.append(record)


def enrich_from_wos(target, r, fresh=False):
    remember_source_aliases(target, r)
    addsrc(target, 'wos')
    incoming_sources = r.get('sources') or []
    if isinstance(incoming_sources, str):
        incoming_sources = [incoming_sources]
    for source in [*incoming_sources, r.get('source')]:
        addsrc(target, source)
    append_unique_wos_record(target, r)
    set_missing(target, 'wos_uid', r.get('wos_uid'))
    set_missing(target, 'doi', nd(r.get('doi')))
    set_missing(target, 'url', r.get('url'))
    set_missing(target, 'authors_raw', r.get('authors_raw'))
    set_missing(target, 'venue', r.get('venue') or r.get('venue_en'))
    set_missing(target, 'venue_en', r.get('venue_en') or r.get('venue'))
    set_missing(target, 'publisher', r.get('publisher'))
    set_missing(target, 'volume', r.get('volume'))
    set_missing(target, 'issue', r.get('issue'))
    set_missing(target, 'pages', r.get('pages'))
    set_missing(target, 'issn', r.get('issn'))
    set_missing(target, 'eissn', r.get('eissn'))
    set_missing(target, 'isbn', r.get('isbn'))
    if citation_is_new(r, target, 'wos', fresh) and r.get('wos_citations') is not None:
        target['wos_citations'] = r['wos_citations']
        mark_citation_observation(target, r, 'wos')
    else:
        set_missing(target, 'wos_citations', r.get('wos_citations'))
    set_missing(target, 'references_count', r.get('references_count'))
    set_missing(target, 'publication_type', r.get('document_type'))
    set_lang_field(target, 'title', r.get('title_en') or r.get('title'), 'en')
    set_lang_field(target, 'venue', r.get('venue_en') or r.get('venue'), 'en')


def merge_wos(canon, records, fresh=False):
    by_item, by_title, by_ty, by_doi = indexes(canon)
    aliases = {}
    for position, row in enumerate(canon):
        index_source_aliases(aliases, row, position)
    enriched = added = 0
    for r in records or []:
        doi = nd(r.get('doi'))
        title = nt(r.get('title_en') or r.get('title'))
        position, ambiguous = match_source_aliases(aliases, r)
        if ambiguous:
            continue
        target = canon[position] if position is not None else None
        if target is None and title:
            target = by_ty.get((title, str(r.get('year') or ''))) or by_title.get(title)
            # Same titles cannot override contradictory provider identities.
            if target is not None and conflicting_source_identity(target, r):
                target = None
        if target:
            enrich_from_wos(target, r, fresh=fresh)
            if position is None:
                position = next(i for i, row in enumerate(canon) if row is target)
            index_source_aliases(aliases, target, position)
            enriched += 1
        else:
            rec = {
                'source': 'wos_free_view_auto',
                'number': None,
                'elibrary_item_id': None,
                'year': r.get('year'),
                'rinc_citations': None,
                'title': r.get('title_en') or r.get('title'),
                'title_en': r.get('title_en') or r.get('title'),
                'title_en_source': 'web_of_science',
                'authors_raw': r.get('authors_raw') or '',
                'venue': r.get('venue') or r.get('venue_en'),
                'venue_en': r.get('venue_en') or r.get('venue'),
                'venue_en_source': 'web_of_science',
                'publisher': r.get('publisher'),
                'volume': r.get('volume'),
                'issue': r.get('issue'),
                'pages': r.get('pages') if usable_source_pages(r.get('pages')) else None,
                'doi': doi,
                'url': r.get('url'),
                'wos_uid': r.get('wos_uid'),
                'wos_citations': r.get('wos_citations'),
                'references_count': r.get('references_count'),
                'publication_type': r.get('document_type'),
                'sources': ['wos'],
                'wos_records': [r],
                'auto_accept_reason': 'author-scoped Web of Science ResearcherID record',
            }
            remember_source_aliases(rec, r)
            for source in ([r['sources']] if isinstance(r.get('sources'), str) else r.get('sources') or []):
                addsrc(rec, source)
            addsrc(rec, r.get('source'))
            mark_citation_observation(rec, r, 'wos')
            enrich_localized_fields(rec)
            canon.append(rec)
            index_source_aliases(aliases, rec, len(canon) - 1)
            added += 1
            if doi:
                by_doi[doi] = rec
            if title:
                by_title[title] = rec
                by_ty[(title, str(rec.get('year') or ''))] = rec
    return enriched, added


def wos_metric(wos_profile, kind):
    summary = (wos_profile or {}).get('summary') or {}
    core = (wos_profile or {}).get('core_collection_metrics') or {}
    return first_number(summary.get(kind), normalized_wos_summary({}, core).get(kind))


def build_scientometrics(canon, elib_profile, scopus_metrics, wos_profile, health=None, previous=None):
    health = health or {}
    previous_sources = (previous or {}).get('sources') or {}
    gm = (elib_profile or {}).get('general_metrics') or {}
    sm = scopus_metrics or {}
    official = sm.get('profile') or {}
    author_valid = sm.get('author_profile_status') == 200 and bool(official)
    calculated = {
        'publications': as_number(sm.get('works_count_from_search')),
        'citations': as_number(sm.get('citation_sum_from_search')),
        'h_index': as_number(sm.get('h_index_recomputed_from_retrieved_works')),
    }
    sc_values = {}
    sc_methods = {}
    for key, profile_key in [('publications', 'document_count'), ('citations', 'citation_count'), ('h_index', 'h_index')]:
        value = as_number(official.get(profile_key)) if author_valid else None
        sc_values[key] = value if value is not None else calculated[key]
        sc_methods[key] = 'official_author_profile' if value is not None else 'calculated_from_complete_search'
    values = {
        'rinc': {'publications': first_number(metric_value(gm, 'Число публикаций в РИНЦ'), (elib_profile or {}).get('summary', {}).get('publications_rinc')),
                 'citations': first_number(metric_value(gm, 'Число цитирований из публикаций, входящих в РИНЦ'), (elib_profile or {}).get('summary', {}).get('citations_rinc')),
                 'h_index': first_number(metric_value(gm, 'Индекс Хирша по публикациям в РИНЦ'), (elib_profile or {}).get('summary', {}).get('h_index_rinc'))},
        'scopus': sc_values,
        'wos': {key: wos_metric(wos_profile, key) for key in ('publications', 'citations', 'h_index')},
    }
    labels = {'rinc': ('РИНЦ', 'RSCI', 'eLibrary/РИНЦ', 'elibrary'),
              'scopus': ('Scopus', 'Scopus', 'Scopus API', 'scopus'),
              'wos': ('Web of Science', 'Web of Science', 'Web of Science Researcher Profile', 'wos')}
    sources = {}
    for name, (ru, en, source, provider) in labels.items():
        state = component_state(health.get(provider), 'metrics') or normalize_health({})
        old = previous_sources.get(name) or {}
        metrics = values[name]
        observed = {
            key: state.get('last_success_at') if is_fresh(state) and value is not None
            else (old.get('metric_observed_at') or {}).get(key, old.get('last_success_at') or state.get('last_success_at'))
            for key, value in metrics.items()
        }
        retained = [key for key, value in metrics.items() if not is_fresh(state) or value is None]
        if not is_fresh(state):
            # An error payload must never become a new zero-valued observation.
            metrics = {key: old.get(key) for key in ('publications', 'citations', 'h_index')}
        else:
            metrics = {key: value if value is not None else old.get(key) for key, value in metrics.items()}
        method = old.get('method', sc_methods if name == 'scopus' else 'provider_profile')
        if is_fresh(state):
            if name == 'scopus':
                method = sc_methods
            elif name == 'wos':
                method = copy.deepcopy((wos_profile or {}).get('metric_methods') or 'provider_profile')
        sources[name] = {'label_ru': ru, 'label_en': en, 'source': source, **metrics, **state,
                         'metric_observed_at': observed, 'retained_metrics': retained,
                         'method': method}
    return {'generated_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            'columns': ['rinc', 'scopus', 'wos'],
            'rows': [{'key': 'publications', 'label_ru': 'Количество публикаций', 'label_en': 'Publications'},
                     {'key': 'citations', 'label_ru': 'Количество цитирований', 'label_en': 'Citations'},
                     {'key': 'h_index', 'label_ru': 'H-индекс (Хирш)', 'label_en': 'H-index'}],
            'sources': sources}


def write_tsv(pubs):
    with (PUBLIC / 'publications.tsv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['number', 'year', 'rinc_citations', 'scopus_citations', 'title', 'title_ru', 'title_en', 'authors', 'venue', 'venue_ru', 'venue_en', 'volume', 'issue', 'pages', 'doi', 'url', 'sources'])
        for p in pubs:
            enrich_localized_fields(p)
            sources = p.get('sources') or []
            if isinstance(sources, str):
                sources = [sources]
            w.writerow([p.get('number'), p.get('year'), p.get('rinc_citations', 0), (p.get('scopus') or {}).get('cited_by_count', ''), p.get('title'), p.get('title_ru', ''), p.get('title_en', ''), p.get('authors_raw'), p.get('venue'), p.get('venue_ru', ''), p.get('venue_en', ''), p.get('volume', ''), p.get('issue', ''), p.get('pages', ''), p.get('doi', ''), p.get('url'), ','.join(sources)])


def ensure_queue():
    # Existing editorial decisions are durable data, not rebuildable output.
    queue = DATA / 'admin_queue'
    queue.mkdir(parents=True, exist_ok=True)
    if not (queue / 'publications.json').exists():
        write_json(queue / 'publications.json', [])
    if not (queue / 'publications.csv').exists():
        with (queue / 'publications.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerow(['id', 'entity_type', 'action', 'confidence', 'reason', 'title', 'year', 'doi', 'source'])


def previous_scientometrics(public_profile, legacy_metrics):
    """Migrate known historical totals without inventing a fresh observation."""
    result = copy.deepcopy(public_profile.get('scientometrics') or {})
    sources = result.setdefault('sources', {})
    for name, legacy in [('rinc', 'risc'), ('scopus', 'scopus'), ('wos', 'wos')]:
        source = sources.setdefault(name, {})
        for key in ('publications', 'citations', 'h_index'):
            set_missing(source, key, (legacy_metrics.get(legacy) or {}).get(key))
    return result


def update_legacy_metrics(previous, scientometrics, elib_profile, health):
    """Keep the site's metrics schema and change values only after verification."""
    result = copy.deepcopy(previous)
    for name, legacy, provider in [('rinc', 'risc', 'elibrary'), ('scopus', 'scopus', 'scopus'), ('wos', 'wos', 'wos')]:
        if not is_fresh(component_state(health.get(provider), 'metrics')):
            continue
        metrics = result.setdefault(legacy, {})
        for key in ('publications', 'citations', 'h_index'):
            value = scientometrics['sources'][name].get(key)
            if value is not None:
                metrics[key] = value
    if is_fresh(component_state(health.get('elibrary'), 'metrics')):
        summary = (elib_profile or {}).get('summary') or {}
        for key, field in [('core_publications', 'publications_core_rinc'), ('core_citations', 'citations_core_rinc'), ('core_h_index', 'h_index_core_rinc')]:
            if summary.get(field) is not None:
                result.setdefault('risc', {})[key] = summary[field]
        if elib_profile.get('elibrary_updated_at'):
            result.setdefault('metadata', {})['elibrary_updated'] = elib_profile['elibrary_updated_at']
    return result


def main():
    recover_source_checkpoints()
    prof = profile()
    ids = prof.get('identifiers', {}) or {}
    sid = ids.get('scopus_author_id', '')
    previous_profile = read_json(PUBLIC / 'profile.json', {})
    legacy_metrics = read_json(PUBLIC / 'metrics.json', {})
    health = load_source_health(ids, previous_profile.get('source_health'))
    canon = load_elib(ids)
    scopus_metrics = read_json(DATA / f'scopus/scopus_author_{sid}_metrics.json', None) if sid else None
    scopus_works = read_json(DATA / f'scopus/scopus_author_{sid}_works.json', []) if sid else []
    scopus_added = merge_scopus(canon, scopus_works, fresh=is_fresh(component_state(health['scopus'], 'publications')))
    open_records = (read_json(DATA / 'open/open_publications.json', {}) or {}).get('records', [])
    open_enriched, open_added = merge_open(canon, open_records)
    wos_profile = read_json(DATA / 'wos/profile_metrics.json', {})
    wos_records = (wos_profile or {}).get('records', [])
    wos_enriched, wos_added = merge_wos(canon, wos_records, fresh=is_fresh(component_state(health['wos'], 'publications')))
    elib_profile = read_json(DATA / 'elibrary/profile_metrics.json', {})
    for p in canon:
        enrich_localized_fields(p)
        if p.get('url'):
            p['url'] = clean_url(p['url'])
    scientometrics = build_scientometrics(canon, elib_profile, scopus_metrics, wos_profile, health,
                                        previous_scientometrics(previous_profile, legacy_metrics))
    public_profile = {
        **copy.deepcopy(previous_profile),
        'generated_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'name_ru': prof.get('display_name_ru', ''),
        'name_en': prof.get('display_name_en', ''),
        'identifiers': ids,
        'elibrary_metrics': read_json(DATA / 'elibrary/metrics.json', {}),
        'elibrary_profile_metrics': elib_profile,
        'wos_profile_metrics': wos_profile,
        'scopus_metrics': scopus_metrics,
        'scientometrics': scientometrics,
        'open_sources_report': read_json(DATA / 'open/harvest_report.json', {}),
        'canonical_publications_count': len(canon),
        'scopus_enriched_publications_count': sum(1 for p in canon if 'scopus' in p.get('sources', [])),
        'scopus_auto_added_publications_count': scopus_added,
        'open_sources_records_count': len(open_records),
        'open_sources_enriched_publications_count': open_enriched,
        'open_sources_auto_added_publications_count': open_added,
        'wos_records_count': len(wos_records),
        'wos_enriched_publications_count': wos_enriched,
        'wos_auto_added_publications_count': wos_added,
        'admin_queue_size': len(read_json(DATA / 'admin_queue/publications.json', [])),
        'source_health': health,
    }
    write_json(PUBLIC / 'profile.json', public_profile)
    updated_metrics = update_legacy_metrics(legacy_metrics, scientometrics, elib_profile, health)
    if updated_metrics != legacy_metrics:
        write_json(PUBLIC / 'metrics.json', updated_metrics)
    write_json(PUBLIC / 'publications.json', canon)
    write_tsv(canon)
    ensure_queue()
    print(f'Built public data: {len(canon)} canonical publications; prior records and queue retained')


if __name__ == '__main__':
    main()
