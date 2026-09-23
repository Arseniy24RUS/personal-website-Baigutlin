import copy
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import build_public_data as builder
import guard_profile_cache as guard
import audit_refresh_pipeline as audit
import merge_wos_records_into_public_data as wos_merge
import validate_retention as retention

FRESH = {'status': 'success', 'origin': 'live', 'complete': True,
         'attempted_at': '2026-09-20T10:00:00Z', 'last_success_at': '2026-09-20T10:00:00Z'}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class PublicationsTest(unittest.TestCase):
    def test_preserves_distinct_published_duplicates_and_manual_metadata(self):
        old = [{'id': 'one', 'doi': '10.1/example', 'title': 'Manual title', 'title_en': 'Manual English'},
               {'id': 'two', 'doi': '10.1/example', 'title': 'Second published card'}]
        result = builder.merge_publication_sets(old, [{'doi': '10.1/example', 'title': 'Provider title', 'pages': '1-8'}])
        self.assertEqual(len(result), 2)
        self.assertEqual({row['id'] for row in result}, {'one', 'two'})
        self.assertEqual(result[0]['title_en'], 'Manual English')
        self.assertEqual(result[0]['pages'], '1-8')
        self.assertEqual(old[0].get('pages'), None)  # no mutation of the baseline

    def test_all_provider_failure_preserves_full_real_publication_baseline(self):
        original = json.loads((REPO / 'data/public/publications.json').read_text(encoding='utf-8'))
        profile = json.loads((REPO / 'data/public/profile.json').read_text(encoding='utf-8'))
        configured_profile = builder.profile()
        legacy_metrics = json.loads((REPO / 'data/public/metrics.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / 'data'
            dump(data / 'public/metrics.json', legacy_metrics)
            dump(data / 'public/publications.json', original)
            dump(data / 'public/profile.json', profile)
            queue = [{'id': 'editorial-review', 'title': 'Keep this pending publication'}]
            dump(data / 'admin_queue/publications.json', queue)
            csv_path = data / 'admin_queue/publications.csv'
            csv_path.write_text('existing,review\n', encoding='utf-8')
            with patch.object(builder, 'DATA', data), patch.object(builder, 'PUBLIC', data / 'public'), patch.object(builder, 'profile', return_value=configured_profile):
                builder.main()
            after = json.loads((data / 'public/publications.json').read_text(encoding='utf-8'))
            self.assertEqual(len(after), len(original))
            self.assertEqual(retention.compare_records(original, after, 'publications'), [])
            current = json.loads((data / 'public/profile.json').read_text(encoding='utf-8'))
            for provider in ('rinc', 'scopus', 'wos'):
                for metric in ('publications', 'citations', 'h_index'):
                    self.assertEqual(current['scientometrics']['sources'][provider][metric], legacy_metrics['risc' if provider == 'rinc' else provider][metric])
            self.assertEqual(json.loads((data / 'admin_queue/publications.json').read_text()), queue)
            self.assertEqual(csv_path.read_text(), 'existing,review\n')

    def test_verified_zero_citations_update_and_stale_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            dump(data / 'public/publications.json', [{'elibrary_item_id': '1', 'title': 'Kept', 'rinc_citations': 4}])
            dump(data / 'public/profile.json', {'identifiers': {}})
            dump(data / 'processed/elibrary_publications.json', [{'elibrary_item_id': '1', 'title': 'Kept', 'rinc_citations': 0}])
            dump(data / 'elibrary/browser_fetch_report.json', FRESH)
            with patch.object(builder, 'DATA', data):
                self.assertEqual(builder.load_elib({})[0]['rinc_citations'], 0)
                dump(data / 'elibrary/browser_fetch_report.json', {**FRESH, 'status': 'blocked', 'origin': 'snapshot'})
                self.assertEqual(builder.load_elib({})[0]['rinc_citations'], 4)
        pub = {'title': 'Kept', 'wos_citations': 8}
        wos_merge.enrich_existing(pub, {'title': 'Provider', 'wos_citations': 0}, fresh=True)
        self.assertEqual(pub['wos_citations'], 0)

    def test_scopus_official_nested_metrics_and_zero_take_priority(self):
        data = {'author_profile_status': 200, 'profile': {'document_count': 11, 'citation_count': 0, 'cited_by_count': 8, 'h_index': 0},
                'works_count_from_search': 9, 'citation_sum_from_search': 12, 'h_index_recomputed_from_retrieved_works': 3}
        sources = builder.build_scientometrics([], {}, data, {}, {'scopus': FRESH})['sources']
        self.assertEqual((sources['scopus']['publications'], sources['scopus']['citations'], sources['scopus']['h_index']), (11, 0, 0))
        self.assertEqual(sources['scopus']['method']['citations'], 'official_author_profile')
        data['author_profile_status'] = 401
        result = builder.build_scientometrics([], {}, data, {}, {'scopus': FRESH})['sources']['scopus']
        self.assertEqual(result['citations'], 12)
        self.assertEqual(result['method']['citations'], 'calculated_from_complete_search')

    def test_incomplete_search_cannot_zero_previous_metrics(self):
        old = {'sources': {'scopus': {'publications': 8, 'citations': 7, 'h_index': 2}}}
        result = builder.build_scientometrics([], {}, {'works_count_from_search': 0, 'citation_sum_from_search': 0}, {},
                                             {'scopus': {**FRESH, 'status': 'partial', 'complete': False}}, old)
        self.assertEqual(result['sources']['scopus']['citations'], 7)

    def test_guard_mismatch_does_not_delete_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'published.json'
            path.write_text('retained')
            report = {}
            guard.unlink(path, report, 'identifier_mismatch')
            self.assertEqual(path.read_text(), 'retained')
            self.assertEqual(len(report['errors']), 1)

    def test_audit_never_calls_blocked_collection_successful(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            dump(data / 'public/publications.json', [{'id': 'one', 'title': 'Kept'}])
            dump(data / 'public/profile.json', {'identifiers': {}, 'canonical_publications_count': 1})
            report = data / 'audit.json'
            with patch.object(audit, 'DATA', data), patch.object(audit, 'REPORT', report), patch.object(audit, 'load_profile_config', return_value={}), patch.object(audit, 'load_source_health', return_value={'wos': {**FRESH, 'status': 'blocked', 'origin': 'snapshot'}}):
                self.assertEqual(audit.main(), 2)
            self.assertEqual(json.loads(report.read_text())['status'], 'partial')

    def test_success_without_observation_date_is_not_fresh(self):
        self.assertFalse(builder.is_fresh({'status': 'success', 'origin': 'live', 'complete': True}))


class RetentionTest(unittest.TestCase):
    def test_equal_count_different_identity_is_rejected(self):
        before = [{'id': 'one', 'title': 'A'}, {'id': 'two', 'title': 'B'}]
        after = [{'id': 'one', 'title': 'A'}, {'id': 'three', 'title': 'C'}]
        self.assertIn('record_removed', {issue['code'] for issue in retention.compare_records(before, after, 'publications')})

    def test_duplicates_are_matched_one_to_one(self):
        before = [{'doi': '10/x', 'title': 'A'}, {'doi': '10/x', 'title': 'A'}]
        issues = retention.compare_records(before, before[:1], 'publications')
        self.assertEqual([issue['code'] for issue in issues], ['record_removed'])

    def test_citation_change_allowed_but_localized_copy_protected(self):
        before = [{'id': 'one', 'title_ru': 'Редакторский текст', 'rinc_citations': 8}]
        self.assertEqual(retention.compare_records(before, [{**before[0], 'rinc_citations': 0}], 'publications'), [])
        issues = retention.compare_records(before, [{**before[0], 'title_ru': 'Заменён'}], 'publications')
        self.assertEqual(issues[0]['field'], 'title_ru')

    def test_git_baseline_checks_asset_files_and_record_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump(root / 'data/diplomas/gallery.json', {'items': [{'id': 'one', 'thumb': 'assets/old.webp'}]})
            (root / 'assets').mkdir()
            asset = root / 'assets/old.webp'
            asset.write_bytes(b'old-image')
            for args in [('init', '-q'), ('add', '.'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline')]:
                subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            self.assertEqual(retention.validate(root, 'HEAD')['status'], 'success')
            asset.unlink()
            self.assertEqual(retention.validate(root, 'HEAD')['status'], 'error')

    def test_git_baseline_rejects_changed_asset_bytes_but_allows_additions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = {
                'assets/старое фото.bin': b'\x00\x01original-image',
                'data/risi/articles/article.html': b'<p>Published article</p>\n',
                'content/risi/reference.txt': b'Published text\nSecond line\n',
            }
            for name, content in assets.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for args in [('init', '-q'), ('config', 'core.autocrlf', 'true'),
                         ('add', '.'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline')]:
                subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            (root / 'assets/new.bin').write_bytes(b'newly published asset')
            # Git-normalized Windows checkouts must not look like content edits.
            text_path = root / 'content/risi/reference.txt'
            text_path.write_bytes(assets['content/risi/reference.txt'].replace(b'\n', b'\r\n'))
            result = retention.validate(root, 'HEAD')
            self.assertEqual(result['status'], 'success', result['issues'])
            self.assertEqual(result['assets_content_checked'], 3)
            for name, content in assets.items():
                with self.subTest(asset=name):
                    target = root / name
                    target.write_bytes(content + b'corrupted or replaced content')
                    result = retention.validate(root, 'HEAD')
                    self.assertEqual(result['status'], 'error')
                    self.assertIn({'path': name, 'code': 'published_asset_modified'},
                                  [{'path': item.get('path'), 'code': item['code']} for item in result['issues']])
                    target.write_bytes(content)


if __name__ == '__main__':
    unittest.main()
