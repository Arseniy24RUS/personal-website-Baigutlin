"""Translation timeouts must remain bounded and preserve publishable text."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import translation_runtime as runtime
import translate_publication_titles as publications
from media_translation import MediaTranslator


def command(code):
    return [sys.executable, '-u', '-c', code]


READY = "import json,sys; print(json.dumps({'status':'ready','probe_passed':True}),flush=True); "


class TranslationRuntimeTests(unittest.TestCase):
    def test_hanging_initialization_stops_once_and_never_retries_per_title(self):
        translator = runtime.BoundedArgosTranslator(init_timeout=.3, total_budget=5,
            worker_command=command('import time; time.sleep(30)'))
        self.addCleanup(translator.close)
        original_popen = runtime.subprocess.Popen
        started = time.monotonic()
        with patch.object(runtime.subprocess, 'Popen', wraps=original_popen) as spawn:
            self.assertFalse(translator.ensure())
            self.assertEqual(translator.translate('Русский заголовок'), '')
            self.assertFalse(translator.ensure())
            self.assertEqual(spawn.call_count, 1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(translator.status, 'model_initialization_timeout')
        self.assertIsNone(translator._process)

    def test_hanging_inference_stops_worker_and_preserves_fallback(self):
        translator = runtime.BoundedArgosTranslator(init_timeout=3, item_timeout=.2, total_budget=5,
            worker_command=command(READY + "sys.stdin.readline(); import time; time.sleep(30)"))
        self.addCleanup(translator.close)
        self.assertTrue(translator.ensure())
        self.assertEqual(translator.translate('Сохранить русский текст'), '')
        self.assertEqual(translator.status, 'translation_timeout')
        self.assertEqual(translator.translate('Другой текст'), '')
        self.assertIsNone(translator._process)

    def test_persistent_worker_success_and_run_budget(self):
        worker = READY + "\nfor line in sys.stdin: print(json.dumps({'status':'translated','text':'Scientific research'}),flush=True)"
        translator = runtime.BoundedArgosTranslator(init_timeout=3, total_budget=5, worker_command=command(worker))
        self.addCleanup(translator.close)
        self.assertEqual(translator.translate('Исследование'), 'Scientific research')
        process = translator._process
        self.assertEqual(translator.translate('Научная работа'), 'Scientific research')
        self.assertIs(translator._process, process)
        translator._deadline = time.monotonic() - 1
        self.assertEqual(translator.translate('Ещё текст'), '')
        self.assertEqual(translator.status, 'translation_budget_exceeded')

    def test_unavailable_worker_cannot_trigger_repeated_initialization(self):
        translator = runtime.BoundedArgosTranslator(worker_command=command(
            "print('{\"status\":\"unavailable_ru_en_model_unavailable\"}',flush=True)"))
        self.addCleanup(translator.close)
        self.assertFalse(translator.ensure())
        self.assertFalse(translator.ensure())
        self.assertEqual(translator.status, 'unavailable_ru_en_model_unavailable')
        self.assertIsNone(translator._process)

    def test_shared_model_environment_overrides_only_worker_cache_locations(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            'PORTFOLIO_TRANSLATION_HOME': temporary, 'XDG_DATA_HOME': '/browser/data',
            'XDG_CACHE_HOME': '/browser/cache', 'XDG_CONFIG_HOME': '/browser/config'}):
            environment = runtime.worker_environment()
            root = Path(temporary).resolve()
            self.assertEqual(environment['ARGOS_PACKAGES_DIR'], str(root / 'packages'))
            self.assertEqual(environment['XDG_DATA_HOME'], str(root / 'data'))
            self.assertEqual(environment['XDG_CACHE_HOME'], str(root / 'cache'))
            self.assertEqual(environment['ARGOS_DEVICE_TYPE'], 'cpu')
            self.assertEqual(os.environ['XDG_CACHE_HOME'], '/browser/cache')

    def test_media_model_failure_keeps_manual_and_russian_values(self):
        backend = Mock(status='model_initialization_timeout')
        backend.ensure.return_value = False
        with tempfile.TemporaryDirectory() as temporary, patch('media_translation.BoundedArgosTranslator', return_value=backend) as factory:
            translator = MediaTranslator(Path(temporary) / 'cache.json')
            records = [{'title_ru': 'Первая новость', 'title_en': 'Manual English', 'description_ru': 'Описание'},
                       {'title_ru': 'Вторая новость'}]
            for row in records:
                translator.enrich(row)
            translator.save()
            self.assertEqual(records[0]['title_en'], 'Manual English')
            self.assertEqual(records[0]['description_ru'], 'Описание')
            self.assertNotIn('description_en', records[0])
            self.assertNotIn('title_en', records[1])
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(backend.ensure.call_count, 1)
            self.assertEqual(translator.status, 'model_initialization_timeout')

    def test_publication_failure_writes_report_and_retains_all_text(self):
        translator = publications.ArgosTranslator(worker_command=command(
            "print('{\"status\":\"unavailable_ru_en_model_unavailable\"}',flush=True)"))
        self.addCleanup(translator.close)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = [{'id': 'one', 'title': 'Исследование', 'title_en': 'Manual English'},
                        {'id': 'two', 'title': 'Новая работа'}, {'id': 'three', 'title': 'Другая работа'}]
            data = root / 'publications.json'
            data.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
            with patch.object(publications, 'PUBLICATIONS_JSON', data), \
                 patch.object(publications, 'PUBLICATIONS_TSV', root / 'publications.tsv'), \
                 patch.object(publications, 'TRANSLATIONS_JSON', root / 'cache.json'), \
                 patch.object(publications, 'REPORT_JSON', root / 'report.json'), \
                 patch.object(publications, 'ArgosTranslator', return_value=translator), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(publications.main(), 0)
            result = json.loads(data.read_text(encoding='utf-8'))
            for old, new in zip(original, result):
                for key, value in old.items():
                    self.assertEqual(new[key], value)
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(report['stats']['unresolved'], 2)
            self.assertEqual(report['argos_status'], 'unavailable_ru_en_model_unavailable')

    def fake_argos_modules(self, package, settings, translate):
        module = types.ModuleType('argostranslate')
        module.package, module.settings, module.translate = package, settings, translate
        return {'argostranslate': module, 'argostranslate.package': package,
                'argostranslate.settings': settings, 'argostranslate.translate': translate}

    def test_provision_missing_index_does_not_enter_argos_recursive_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Mock()
            package.get_installed_packages.return_value = []
            settings = types.SimpleNamespace(local_package_index=Path(temporary) / 'missing.json')
            with patch.dict(sys.modules, self.fake_argos_modules(package, settings, Mock())), \
                 patch.object(runtime.socket, 'setdefaulttimeout'):
                with self.assertRaisesRegex(RuntimeError, 'model_index_unavailable'):
                    runtime.load_translation(allow_download=True)
            package.update_package_index.assert_called_once()
            package.get_available_packages.assert_not_called()

    def test_offline_worker_blocks_lazy_model_network_requests(self):
        package = Mock()
        package.get_installed_packages.side_effect = lambda: runtime.socket.create_connection(('example.invalid', 443))
        with patch.dict(sys.modules, self.fake_argos_modules(package, Mock(), Mock())), \
             patch.object(runtime.socket.socket, 'connect'), \
             patch.object(runtime.socket, 'create_connection'), \
             patch.object(runtime.socket, 'getaddrinfo'):
            with self.assertRaisesRegex(OSError, 'installed models only'):
                runtime.load_translation(allow_download=False)
        package.update_package_index.assert_not_called()

    def test_setup_failure_is_visible_degraded_exit_and_report(self):
        backend = Mock(status='model_initialization_timeout', metadata={})
        backend.ensure.return_value = False
        with tempfile.TemporaryDirectory() as temporary, patch.object(runtime, 'BoundedArgosTranslator', return_value=backend), contextlib.redirect_stdout(io.StringIO()):
            report = Path(temporary) / 'setup.json'
            self.assertEqual(runtime.main(['provision', '--root', temporary, '--timeout', '1', '--report', str(report)]), 2)
            result = json.loads(report.read_text())
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['reason'], 'model_initialization_timeout')


if __name__ == '__main__':
    unittest.main()
