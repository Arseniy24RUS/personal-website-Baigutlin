#!/usr/bin/env python3
"""Local eLibrary collector for a trusted home device.

This script is intended to run on a device inside the user's home network
(Android/Termux, Raspberry Pi, mini-PC, or another always-on local device), not
on a GitHub-hosted runner. It performs a small number of requests to the user's
own public eLibrary author pages, saves the HTML snapshots, parses them, and
writes the normalized JSON files consumed by the website.

It does not solve or bypass captcha/challenge pages. If eLibrary returns a
challenge page, the script stops and writes a report explaining that live fetch
was not usable.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_elibrary_author_items import parse_elibrary_author_items  # noqa: E402
from parse_elibrary_author_profile import parse_elibrary_author_profile_html  # noqa: E402

AUTHOR_ID = os.environ.get("ELIBRARY_AUTHOR_ID", "1170779")
ITEMS_URL = os.environ.get(
    "ELIBRARY_ITEMS_URL",
    f"https://elibrary.ru/author_items.asp?authorid={AUTHOR_ID}&pubrole=100&show_refs=1&pubcat=risc",
)
PROFILE_URL = os.environ.get(
    "ELIBRARY_PROFILE_URL",
    f"https://elibrary.ru/author_profile.asp?id={AUTHOR_ID}",
)
DEFAULT_URL = os.environ.get("ELIBRARY_PREFLIGHT_URL", "https://elibrary.ru/defaultx.asp")
OUT_ITEMS = Path(os.environ.get("ELIBRARY_ITEMS_OUT", "data/processed/elibrary_publications.json"))
OUT_PROFILE = Path(os.environ.get("ELIBRARY_PROFILE_OUT", "data/elibrary/profile_metrics.json"))
REPORT = Path(os.environ.get("ELIBRARY_LOCAL_REPORT", "data/elibrary/local_agent_report.json"))
ITEMS_SNAPSHOT_DIR = Path(os.environ.get("ELIBRARY_ITEMS_SNAPSHOT_DIR", "data/snapshots/elibrary/items"))
PROFILE_SNAPSHOT_DIR = Path(os.environ.get("ELIBRARY_PROFILE_SNAPSHOT_DIR", "data/snapshots/elibrary/profile"))
TIMEOUT = int(os.environ.get("ELIBRARY_LOCAL_TIMEOUT", "90"))

HEADERS = {
    "User-Agent": os.environ.get(
        "ELIBRARY_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 YaBrowser/26.4.0.0 Safari/537.36",
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(html: str | None, url: str) -> dict:
    text = html or ""
    lower = text.lower()
    return {
        "url": url,
        "content_length": len(text.encode("utf-8", errors="replace")),
        "has_author_items": "author_items" in text,
        "has_rows": "arw" in text,
        "has_profile_metrics": "ОБЩИЕ ПОКАЗАТЕЛИ" in text and "Индекс Хирша" in text,
        "has_challenge": "тест тьюринга" in lower or "page_captcha" in lower or "recaptcha" in lower,
        "excerpt": text[:900].replace("\n", " "),
    }


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def fetch(opener_obj: urllib.request.OpenerDirector, url: str, referer: str | None = None) -> tuple[str | None, dict]:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    cookie = os.environ.get("ELIBRARY_COOKIE")
    if cookie:
        headers["Cookie"] = cookie
    started = time.time()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with opener_obj.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            enc = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(enc, errors="replace")
            return text, {
                "status": "ok",
                "http_status": resp.status,
                "elapsed_sec": round(time.time() - started, 3),
                "final_url": resp.geturl(),
                "bytes": len(raw),
                "fingerprint": fingerprint(text, url),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1200]
        return None, {
            "status": "http_error",
            "http_status": exc.code,
            "elapsed_sec": round(time.time() - started, 3),
            "error_excerpt": body,
            "fingerprint": fingerprint(body, url),
        }
    except Exception as exc:
        return None, {
            "status": "error",
            "elapsed_sec": round(time.time() - started, 3),
            "error_type": type(exc).__name__,
            "error": repr(exc),
            "fingerprint": fingerprint(None, url),
        }


def save_items(html: str) -> dict:
    ITEMS_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = ITEMS_SNAPSHOT_DIR / f"author_items_{AUTHOR_ID}_{stamp}_local.html"
    snapshot.write_text(html, encoding="utf-8")
    records = parse_elibrary_author_items(str(snapshot))
    write_json(OUT_ITEMS, records)
    return {"snapshot_path": str(snapshot), "records": len(records), "out": str(OUT_ITEMS)}


def save_profile(html: str) -> dict:
    PROFILE_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = PROFILE_SNAPSHOT_DIR / f"author_profile_{AUTHOR_ID}_{stamp}_local.html"
    snapshot.write_text(html, encoding="utf-8")
    data = parse_elibrary_author_profile_html(html)
    write_json(OUT_PROFILE, data)
    return {"snapshot_path": str(snapshot), "out": str(OUT_PROFILE)}


def main() -> int:
    op = opener()
    report = {
        "generated_at": now(),
        "mode": "local_home_agent",
        "cookie_present": bool(os.environ.get("ELIBRARY_COOKIE")),
        "items_url": ITEMS_URL,
        "profile_url": PROFILE_URL,
        "steps": {},
    }

    _, preflight_report = fetch(op, DEFAULT_URL, "https://elibrary.ru/")
    report["steps"]["preflight"] = preflight_report

    items_html, items_report = fetch(op, ITEMS_URL, DEFAULT_URL)
    report["steps"]["items"] = items_report
    ok_items = bool(items_html and "author_items" in items_html and "arw" in items_html)
    if ok_items:
        report["items_result"] = save_items(items_html)
    else:
        report["items_error"] = "eLibrary did not return a parseable author_items page."

    profile_html, profile_report = fetch(op, PROFILE_URL, DEFAULT_URL)
    report["steps"]["profile"] = profile_report
    ok_profile = bool(profile_html and "ОБЩИЕ ПОКАЗАТЕЛИ" in profile_html and "Индекс Хирша" in profile_html)
    if ok_profile:
        report["profile_result"] = save_profile(profile_html)
    else:
        report["profile_error"] = "eLibrary did not return a parseable author_profile page."

    report["status"] = "ok" if ok_items else "items_failed"
    write_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok_items else 2


if __name__ == "__main__":
    raise SystemExit(main())
