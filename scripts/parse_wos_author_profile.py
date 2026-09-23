#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse
import argparse
import hashlib
import json
import re
from typing import Any

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>,;\"'&?#\)\]]+", re.I)
DOCUMENT_TYPES = {'article', 'review', 'proceedings paper', 'статья', 'обзор', 'материалы конференции'}
# Provider terminology: https://webofscience.help.clarivate.com/ru-ru/Content/researcher-profile-metrics.html
CORE_PUBLICATIONS = ('Web of Science Core Collection publications', 'Публикации Web of Science Core Collection',
                     'Публикации в Web of Science Core Collection')
INDEXED_PUBLICATIONS = ('Publications indexed in Web of Science', 'Публикации, индексируемые в Web of Science',
                        'Публикации, индексированные в Web of Science',
                        'Публикации, проиндексированные в Web of Science')


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str("" if value is None else value).replace("\xa0", " ")).strip()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def int_value(value: Any):
    text = clean(value)
    return int(re.sub(r"[\s,]", "", text)) if re.fullmatch(r"-?\d[\d\s,]*", text) else None


def trim_doi(value: Any) -> str | None:
    doi = clean(value)
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I).strip()
    doi = unquote(doi)
    doi = re.split(r"[&#?]", doi, 1)[0]
    doi = doi.rstrip(".,;:)]}").lower()
    return doi or None


def normalize_doi(value: Any) -> str | None:
    return trim_doi(value)


def extract_doi(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    for candidate in (text, unquote(text)):
        m = DOI_RE.search(candidate)
        if m:
            return trim_doi(m.group(0))
    return None


def doi_from_href(href: str) -> str | None:
    href = str(href or "")
    if not href:
        return None
    try:
        qs = parse_qs(urlparse(href).query)
        for key in ("KeyAID", "DestDOI", "DOI", "doi", "DestURL"):
            for value in qs.get(key, []):
                doi = extract_doi(value)
                if doi:
                    return doi
    except Exception:
        pass
    return extract_doi(href)


def normalize_pages(value: Any) -> str | None:
    text = clean(value)
    text = re.sub(r"^[,;]?\s*(?:pp?\.|стр\.|с\.)\s*", "", text, flags=re.I)
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s*-\s*", "-", text).strip(" ,.;")
    return text or None


def extract_year_from_text(value: Any):
    m = re.search(r"\b((?:19|20)\d{2})\b", clean(value))
    return int(m.group(1)) if m else None


def metric_value(mapping: dict, *labels):
    if not isinstance(mapping, dict):
        return None
    for label in labels:
        item = mapping.get(label)
        if isinstance(item, dict) and item.get("value") is not None:
            return item["value"]
    def key(value):
        return re.sub(r'[‐‑–—]', '-', clean(value)).casefold().rstrip(':')
    lowered = {key(k): v for k, v in mapping.items()}
    for label in labels:
        item = lowered.get(key(label))
        if isinstance(item, dict) and item.get("value") is not None:
            return item["value"]
    return None


def parse_summary_items(soup: BeautifulSoup) -> dict:
    out: dict[str, dict] = {}
    for item in soup.select(".summary-item"):
        strings = metric_strings(item)
        if len(strings) >= 2:
            out[" ".join(strings[1:])] = {"raw": strings[0], "value": int_value(strings[0])}
    return out


def parse_core_metrics(soup: BeautifulSoup) -> dict:
    out: dict[str, dict] = {}
    for block in soup.select(".wat-author-metric-inline-block"):
        strings = metric_strings(block)
        if len(strings) >= 2:
            out[" ".join(strings[1:])] = {"raw": strings[0], "value": int_value(strings[0]), "descriptor": " ".join(strings[1:])}
    return out


def metric_strings(node) -> list[str]:
    # Angular Material renders icon names as text in saved DOM snapshots.
    icons = {'help_outline', 'info_outline', 'info', 'help', 'open_in_new'}
    return [clean(s) for s in node.stripped_strings if clean(s) and clean(s) not in icons]


def normalized_summary(summary_metrics: dict, core_metrics: dict) -> dict:
    publications = metric_value(core_metrics, 'Publications', 'Публикации',
                                'Публикации на Web of Science', 'Публикации в Web of Science')
    if publications is None:
        publications = metric_value(summary_metrics, *CORE_PUBLICATIONS)
    # Indexed publications may include other collections, so cannot substitute
    # for a missing Core Collection total. An observed zero is still a value.
    return {
        "publications": publications,
        "citations": metric_value(core_metrics, "Sum of Times Cited", "Суммарное количество цитирований"),
        "h_index": metric_value(core_metrics, "H-Index", "Индекс Хирша", "h-индекс"),
        "total_documents": metric_value(summary_metrics, "Total documents", "Всего документов", "Общее количество документов"),
        "indexed_publications": metric_value(summary_metrics, *INDEXED_PUBLICATIONS),
        "core_collection_publications": metric_value(summary_metrics, *CORE_PUBLICATIONS),
    }


def first_text(node, selectors: list[str]) -> str:
    for selector in selectors:
        el = node.select_one(selector)
        if not el:
            continue
        text = clean(el.get_text(" "))
        if text and text.lower() not in DOCUMENT_TYPES:
            return text
    return ""


def first_nested_title(raw: dict, section: str, lang: str = "en") -> str:
    try:
        values = (((raw.get("titles") or {}).get(section) or {}).get(lang) or [])
        if values:
            return clean(values[0].get("title"))
    except Exception:
        pass
    return ""


def identifiers_dict(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.get("identifiers") or []:
        if isinstance(item, dict) and item.get("type") and item.get("value"):
            out[str(item["type"]).lower()] = clean(item["value"])
    return out


def parse_wosnx_record(raw: dict, position: int | None = None) -> dict:
    pub = raw.get("pub_info") or {}
    ids = identifiers_dict(raw)
    title = first_nested_title(raw, "item")
    venue = first_nested_title(raw, "source")
    venue_abbrev = first_nested_title(raw, "source_abbrev")
    doi = normalize_doi(raw.get("doi") or ids.get("doi"))
    wos_uid = clean(raw.get("colluid") or raw.get("ut") or ((raw.get("id") or {}).get("value") if isinstance(raw.get("id"), dict) else "")) or None
    url = f"https://www.webofscience.com/wos/woscc/full-record/{wos_uid}" if wos_uid else None
    author_items = (((raw.get("names") or {}).get("author") or {}).get("en") or [])
    authors = []
    for author in author_items:
        if isinstance(author, dict):
            authors.append(clean(author.get("wos_standard") or " ".join(x for x in [author.get("last_name"), author.get("first_name")] if x)))
    pages = normalize_pages(pub.get("page_no"))
    if not pages and (pub.get("begin") or pub.get("end")):
        pages = normalize_pages("-".join(x for x in [clean(pub.get("begin")), clean(pub.get("end"))] if x))
    citation_counts = ((raw.get("citation_related") or {}).get("counts") or {}) if isinstance(raw.get("citation_related"), dict) else {}
    fp = hashlib.sha256("|".join([title.lower(), str(pub.get("pubyear") or ""), doi or wos_uid or ""]).encode("utf-8")).hexdigest()[:16]
    return {
        "source": "web_of_science_free_view_author_profile",
        "wos_uid": wos_uid,
        "position": position,
        "document_type": (raw.get("doctypes") or [None])[0],
        "title": title,
        "title_en": title if title and not re.search(r"[А-Яа-яЁё]", title) else "",
        "authors_raw": ", ".join(a for a in authors if a),
        "venue": venue,
        "venue_en": venue,
        "venue_abbrev": venue_abbrev,
        "year": int(pub.get("pubyear")) if str(pub.get("pubyear") or "").isdigit() else extract_year_from_text(pub.get("pubdate") or pub.get("coverdate")),
        "publication_date": clean(pub.get("sortdate") or pub.get("pubdate") or pub.get("coverdate")) or None,
        "volume": clean(pub.get("vol")) or None,
        "issue": clean(pub.get("issue")).strip("()") or None,
        "pages": pages,
        "page_count": int(pub.get("page_count")) if str(pub.get("page_count") or "").isdigit() else None,
        "article_number": ids.get("art_no"),
        "doi": doi,
        "issn": ids.get("issn"),
        "eissn": ids.get("eissn"),
        "isbn": ids.get("isbn") or ids.get("eisbn"),
        "url": url,
        "wos_citations": citation_counts.get("WOSCC") if isinstance(citation_counts, dict) else None,
        "all_database_citations": citation_counts.get("ALLDB") if isinstance(citation_counts, dict) else None,
        "references_count": raw.get("ref_count"),
        "pubtype": pub.get("pubtype"),
        "metadata_raw": clean(" | ".join(str(x) for x in [title, ", ".join(authors), venue, pub.get("pubdate"), pub.get("vol"), pub.get("issue"), pages, doi] if x)),
        "dedupe_fingerprint": fp,
        "sources": ["wos"],
    }


def parse_wosnx_ndjson(text: str, researcher_id: str = "AAN-4717-2020") -> dict:
    records_payload: dict[str, dict] = {}
    search_info: dict = {}
    analyze: dict = {}
    jcr: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        key = obj.get("key")
        payload = obj.get("payload")
        if key == "searchInfo" and isinstance(payload, dict):
            search_info = payload
        elif key == "records" and isinstance(payload, dict):
            records_payload.update(payload)
        elif key == "analyze" and isinstance(payload, dict):
            analyze = payload
        elif key == "jcr" and isinstance(payload, dict):
            jcr = payload
    records = []
    for key in sorted(records_payload, key=lambda x: int(x) if str(x).isdigit() else 999999):
        raw = records_payload[key]
        if isinstance(raw, dict):
            rec = parse_wosnx_record(raw, int(key) if str(key).isdigit() else None)
            if rec.get("title"):
                records.append(rec)
    return {"source": "web_of_science_wosnx_run_query_search", "researcher_id": researcher_id, "generated_at": now(), "search_info": search_info, "records_count_on_page": len(records), "records": records, "analyze": analyze, "jcr": jcr}


def extract_year(record, parts: list[str]):
    pubdate = first_text(record, ['[data-ta="summary-record-pubdate"]', '[name="pubdate"]'])
    year = extract_year_from_text(pubdate)
    if year:
        return year
    for part in parts:
        if len(part) <= 120:
            year = extract_year_from_text(part)
            if year:
                return year
    return None


def parse_record(record) -> dict:
    parts = [clean(x) for x in record.get_text("\n").split("\n") if clean(x)]
    joined = " ".join(parts)
    title_el = record.select_one('app-summary-title a[data-ta="summary-record-title-link"], app-summary-title a, a.title-link')
    title = clean(title_el.get_text(" ")) if title_el else first_text(record, ["app-summary-title .title", ".title-link", "h3 a"])
    if not title:
        candidates = [p for p in parts if len(p) > 18 and not p.isdigit() and p.lower() not in DOCUMENT_TYPES]
        title = candidates[0] if candidates else ""
    doi = extract_doi(joined)
    if not doi:
        for link in record.find_all("a", href=True):
            doi = doi_from_href(link.get("href") or "")
            if doi:
                break
    record_id = None
    url = None
    title_link = title_el or record.find("a", href=True)
    if title_link:
        href = title_link.get("href") or ""
        url = href if href.startswith("http") else "https://www.webofscience.com" + href
        match = re.search(r"WOS:([A-Z0-9]+)", href)
        if match:
            record_id = "WOS:" + match.group(1)
    venue = first_text(record, [".summary-source-title", '[data-ta^="jcrSidenav"][data-ta$="main-header"]'])
    if not venue and "Journal information" in parts:
        idx = parts.index("Journal information")
        for candidate in parts[idx + 1: idx + 8]:
            if candidate.lower() not in {"clear", "publisher name"} and len(candidate) > 4:
                venue = candidate
                break
    publisher = ""
    if "Publisher name" in parts:
        idx = parts.index("Publisher name")
        if idx + 1 < len(parts):
            publisher = parts[idx + 1]
    issue = first_text(record, ['[data-ta="Summary-issue"]']) or None
    if issue:
        issue = issue.strip("()")
    pages = normalize_pages(first_text(record, ['[data-ta="Summary-page-no"]']))
    if not pages:
        match = re.search(r"(?<!\w)(?:pp?\.|стр\.|с\.)\s*([0-9]+\s*[-–—]\s*[0-9]+|[0-9]+)", joined, flags=re.I)
        if match:
            pages = normalize_pages(match.group(1))
    doc_type_node = record.select_one('.doctype-container .data-label, .summary-blueBox.data-label')
    doc_type = clean(doc_type_node.get_text(' ')) if doc_type_node else next((part for part in parts if part.lower() in DOCUMENT_TYPES), None)
    fp = hashlib.sha256("|".join([title.lower(), str(extract_year(record, parts) or ""), doi or record_id or ""]).encode("utf-8")).hexdigest()[:16]
    return {
        "source": "web_of_science_free_view_author_profile",
        "wos_uid": record_id,
        "document_type": doc_type,
        "title": title,
        "title_en": title if title and not re.search(r"[А-Яа-яЁё]", title) else "",
        "authors_raw": first_text(record, ["app-summary-authors"]),
        "venue": venue,
        "venue_en": venue if not re.search(r"[А-Яа-яЁё]", venue) else "",
        "publisher": publisher or None,
        "year": extract_year(record, parts),
        "volume": first_text(record, ['[data-ta="Summary-vol"]']) or None,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "url": url,
        "metadata_raw": clean(" | ".join(parts[:100])),
        "dedupe_fingerprint": fp,
        "sources": ["wos"],
    }


def parse_wos_author_profile_html(html: str, researcher_id: str = "AAN-4717-2020") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = clean(soup.title.get_text(" ") if soup.title else "")
    records = []
    seen = set()
    for node in soup.find_all("app-record"):
        rec = parse_record(node)
        key = rec.get("wos_uid") or rec.get("doi") or (rec.get("title"), rec.get("year"))
        if rec.get("title") and key not in seen:
            seen.add(key)
            records.append(rec)
    summary_metrics = parse_summary_items(soup)
    core_metrics = parse_core_metrics(soup)
    return {"source": "web_of_science_free_view_author_profile", "source_url": f"https://www.webofscience.com/wos/author/record/{researcher_id}", "researcher_id": researcher_id, "generated_at": now(), "page_title": title, "summary": normalized_summary(summary_metrics, core_metrics), "summary_metrics": summary_metrics, "core_collection_metrics": core_metrics, "records_count_on_page": len(records), "records": records}


def parse_file(path: str, researcher_id: str = "AAN-4717-2020") -> dict:
    return parse_wos_author_profile_html(Path(path).read_text(encoding="utf-8", errors="replace"), researcher_id)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--researcher-id", default="AAN-4717-2020")
    ap.add_argument("--out", default="data/wos/profile_metrics.json")
    args = ap.parse_args()
    data = parse_file(args.html, args.researcher_id)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "records": data.get("records_count_on_page"), "researcher_id": data.get("researcher_id")}, ensure_ascii=False, indent=2))
