#!/usr/bin/env python3
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import argparse
import hashlib
import json
import re

GENERAL_LABELS = [
    'Число публикаций на elibrary.ru',
    'Число публикаций в РИНЦ',
    'Число публикаций, входящих в ядро РИНЦ',
    'Число цитирований из публикаций на elibrary.ru',
    'Число цитирований из публикаций, входящих в РИНЦ',
    'Число цитирований из публикаций, входящих в ядро РИНЦ',
    'Индекс Хирша по всем публикациям на elibrary.ru',
    'Индекс Хирша по публикациям в РИНЦ',
    'Индекс Хирша по ядру РИНЦ',
    'Число публикаций, процитировавших работы автора',
    'Число ссылок на самую цитируемую публикацию',
    'Число публикаций автора, процитированных хотя бы один раз',
    'Среднее число цитирований в расчете на одну публикацию',
    'Индекс Хирша без учета самоцитирований',
    'Индекс Хирша по ядру РИНЦ без учета самоцитирований',
    'Индекс Хирша с учетом только статей в журналах',
    'Год первой публикации',
    'Число самоцитирований',
    'Число цитирований соавторами',
    'Число соавторов',
    'Число статей в зарубежных журналах',
    'Число статей в российских журналах',
    'Число статей в российских журналах из перечня ВАК',
    'Число статей в российских переводных журналах',
    'Число статей в журналах с ненулевым импакт-фактором',
    'Число цитирований из зарубежных журналов на публикации автор',
    'Число цитирований из российских журналов',
    'Число цитирований из российских журналов из перечня ВАК',
    'Число цитирований из российских переводных журналов',
    'Число цитирований из журналов с ненулевым импакт-фактором',
    'Средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи',
    'Средневзвешенный импакт-фактор журналов, в которых были процитированы статьи',
    'Число публикаций в РИНЦ за последние 5 лет (2020-2024)',
    'Число публикаций в ядре РИНЦ за последние 5 лет',
    'Число ссылок из РИНЦ на работы, опубликованные за последние 5 лет',
    'Число ссылок из ядра РИНЦ на работы, опубликованные за последние 5 лет',
    'Число ссылок на работы автора из всех публикаций за последние 5 лет',
    'Основная рубрика (ГРНТИ)',
    'Основная рубрика (OECD)',
    'Процентиль по ядру РИНЦ',
    'Участие в публикациях:',
]

YEAR_LABELS = [
    'Число публикаций в РИНЦ',
    'Число публикаций в ядре РИНЦ',
    'Число цитирований в РИНЦ',
    'Число цитирований из ядра РИНЦ',
    'Число публикаций в РИНЦ за 5 лет',
    'Число публикаций в ядре РИНЦ за 5 лет',
    'Число цитирований в РИНЦ за 5 лет',
    'Число цитирований из ядра РИНЦ за 5 лет',
    'Индекс Хирша в РИНЦ',
    'Индекс Хирша по ядру РИНЦ',
    'Процентиль по ядру РИНЦ',
]


def clean(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').replace('\xa0', ' ')).strip()


def parse_number(value):
    if value is None:
        return None
    match = re.search(r'\d+(?:[,.]\d+)?', str(value))
    if not match:
        return None
    raw = match.group(0).replace(',', '.')
    return float(raw) if '.' in raw else int(raw)


def extract_segment(text: str, start_marker: str, end_marker: str | None = None) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ''
    end = text.find(end_marker, start) if end_marker else -1
    return text[start:end if end > start else len(text)]


def extract_ordered_metrics(segment: str, labels: list[str]) -> dict:
    positions = []
    for label in labels:
        pos = segment.find(label)
        if pos >= 0:
            positions.append((pos, label))
    positions.sort()
    metrics = {}
    for idx, (pos, label) in enumerate(positions):
        value_start = pos + len(label)
        value_end = positions[idx + 1][0] if idx + 1 < len(positions) else len(segment)
        value = clean(segment[value_start:value_end])
        value = re.sub(r'^Название показателя\s+Значение\s*', '', value)
        value = re.sub(r'^[:\s]+', '', value)
        if label == 'Участие в публикациях:':
            m = re.search(r'автор\s+(\d+)', value)
            value = m.group(1) if m else value
        metrics[label] = {'raw': value, 'value': parse_number(value)}
    return metrics


def extract_yearly_metrics(segment: str) -> dict:
    header = re.search(r'Название показателя\s+((?:20\d{2}\s+)+)', segment)
    years = header.group(1).split() if header else []
    yearly = {}
    for idx, label in enumerate(YEAR_LABELS):
        pos = segment.find(label)
        if pos < 0:
            continue
        next_positions = [segment.find(next_label, pos + len(label)) for next_label in YEAR_LABELS[idx + 1:]]
        next_positions = [p for p in next_positions if p >= 0]
        value_end = min(next_positions) if next_positions else len(segment)
        values = clean(segment[pos + len(label):value_end]).split()[:len(years)]
        yearly[label] = {year: parse_number(value) for year, value in zip(years, values)}
    return yearly


def extract_affiliations(segment: str) -> list[dict]:
    affiliations = []
    for match in re.finditer(r'([А-ЯA-ZЁ][^0-9]+?\([^()]+\))\s+((?:19|20)\d{2}(?:-(?:19|20)\d{2})?)\s+(\d+)', segment):
        organization = clean(match.group(1))
        if 'Название организации Период Публ.' in organization:
            organization = organization.split('Название организации Период Публ.')[-1].strip()
        affiliations.append({
            'organization': organization,
            'period': match.group(2),
            'publications': int(match.group(3)),
        })
    return affiliations


def parse_elibrary_author_profile_html(html: str, *, source_file_sha256: str | None = None) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    text = clean(soup.get_text(' '))
    general_segment = extract_segment(text, 'ОБЩИЕ ПОКАЗАТЕЛИ', 'ПОКАЗАТЕЛИ ПО ГОДАМ')
    yearly_segment = extract_segment(text, 'ПОКАЗАТЕЛИ ПО ГОДАМ', 'СТАТИСТИЧЕСКИЕ ОТЧЕТЫ')
    affiliation_segment = extract_segment(text, 'МЕСТО РАБОТЫ', 'ОБЩИЕ ПОКАЗАТЕЛИ')
    general = extract_ordered_metrics(general_segment, GENERAL_LABELS)
    yearly = extract_yearly_metrics(yearly_segment)
    updated = re.search(r'Дата обновления показателей автора:\s*(\d{2}\.\d{2}\.\d{4})', text)

    def v(label):
        return (general.get(label) or {}).get('value')

    return {
        'source': 'elibrary_author_profile',
        'source_url': 'https://www.elibrary.ru/author_profile.asp?id=1170779',
        'authorid': '1170779',
        'spin': '6676-3700',
        'author_name': 'Байгутлин Данил Расулович',
        'snapshot_generated_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'source_file_sha256': source_file_sha256,
        'elibrary_updated_at': updated.group(1) if updated else None,
        'summary': {
            'publications_elibrary': v('Число публикаций на elibrary.ru'),
            'publications_rinc': v('Число публикаций в РИНЦ'),
            'publications_core_rinc': v('Число публикаций, входящих в ядро РИНЦ'),
            'citations_elibrary': v('Число цитирований из публикаций на elibrary.ru'),
            'citations_rinc': v('Число цитирований из публикаций, входящих в РИНЦ'),
            'citations_core_rinc': v('Число цитирований из публикаций, входящих в ядро РИНЦ'),
            'h_index_elibrary': v('Индекс Хирша по всем публикациям на elibrary.ru'),
            'h_index_rinc': v('Индекс Хирша по публикациям в РИНЦ'),
            'h_index_core_rinc': v('Индекс Хирша по ядру РИНЦ'),
            'h_index_without_self_citations': v('Индекс Хирша без учета самоцитирований'),
            'h_index_core_without_self_citations': v('Индекс Хирша по ядру РИНЦ без учета самоцитирований'),
            'h_index_journal_articles': v('Индекс Хирша с учетом только статей в журналах'),
            'first_publication_year': v('Год первой публикации'),
            'coauthors_count': v('Число соавторов'),
            'core_rinc_percentile': v('Процентиль по ядру РИНЦ'),
        },
        'general_metrics': general,
        'yearly_metrics': yearly,
        'affiliations': extract_affiliations(affiliation_segment),
    }


def parse_file(path: str) -> dict:
    p = Path(path)
    raw = p.read_bytes()
    html = raw.decode('utf-8', errors='replace')
    return parse_elibrary_author_profile_html(html, source_file_sha256=hashlib.sha256(raw).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('html')
    parser.add_argument('--out', default='data/elibrary/profile_metrics.json')
    args = parser.parse_args()
    data = parse_file(args.html)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Parsed eLibrary author profile metrics -> {out}")
    print(json.dumps(data['summary'], ensure_ascii=False, indent=2))
