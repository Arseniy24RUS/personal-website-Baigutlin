#!/usr/bin/env python3
"""Fetch and parse public eLibrary author profile metrics.

The script first tries a cookie-aware live fetch. In GitHub Actions it can be
routed through the repository OpenVPN step or through ELIBRARY_PROXY_URL. If the
live page is not usable, it falls back to the latest saved HTML snapshot or the
previous normalized JSON so the public site never loses metrics because of a
transient eLibrary block.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_elibrary_author_profile import parse_elibrary_author_profile_html, parse_file  # noqa: E402
from elibrary_fetch import fetch_elibrary_page  # noqa: E402

AUTHOR_ID = os.environ.get("ELIBRARY_AUTHOR_ID", "1170779")
URL = os.environ.get(
    "ELIBRARY_PROFILE_URL",
    f"https://www.elibrary.ru/author_profile.asp?id={AUTHOR_ID}",
)
OUT = Path(os.environ.get("ELIBRARY_PROFILE_OUT", "data/elibrary/profile_metrics.json"))
REPORT = Path(os.environ.get("ELIBRARY_PROFILE_REPORT", "data/elibrary/profile_metrics_fetch_report.json"))
SNAPSHOT_DIR = Path(os.environ.get("ELIBRARY_PROFILE_SNAPSHOT_DIR", "data/snapshots/elibrary/profile"))


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_live() -> tuple[str | None, dict]:
    return fetch_elibrary_page(URL)


def latest_saved_snapshot() -> Path | None:
    if not SNAPSHOT_DIR.exists():
        return None
    files = sorted(SNAPSHOT_DIR.glob("*.html"))
    return files[-1] if files else None


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    html, report = fetch_live()
    report["generated_at"] = now()

    if html:
        lowered = html.lower()
        report["html_fingerprint"] = {
            **(report.get("html_fingerprint") or {}),
            "has_metrics": "ОБЩИЕ ПОКАЗАТЕЛИ" in html,
            "has_h_index": "Индекс Хирша" in html,
            "has_suspicious_ip_text": "подозр" in lowered or "suspicious" in lowered or "ip_blocked" in lowered,
            "has_cookie_text": "cookie" in lowered or "cookies" in lowered,
        }

    if html and "ОБЩИЕ ПОКАЗАТЕЛИ" in html and "Индекс Хирша" in html:
        data = parse_elibrary_author_profile_html(html)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = SNAPSHOT_DIR / f"author_profile_{AUTHOR_ID}_{stamp}.html"
        snapshot_path.write_text(html, encoding="utf-8")
        report["used_source"] = "live_elibrary"
        report["snapshot_path"] = str(snapshot_path)
    else:
        fallback = latest_saved_snapshot()
        if fallback:
            data = parse_file(str(fallback))
            report["used_source"] = "saved_snapshot"
            report["snapshot_path"] = str(fallback)
        elif OUT.exists():
            report["used_source"] = "previous_normalized_json"
            data = json.loads(OUT.read_text(encoding="utf-8"))
        else:
            report["used_source"] = "none"
            report["error"] = "No live profile, no saved HTML snapshot, and no previous normalized JSON."
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(OUT), "used_source": report.get("used_source"), "summary": data.get("summary")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
