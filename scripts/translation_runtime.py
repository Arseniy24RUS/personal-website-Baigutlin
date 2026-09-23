"""Bounded Argos model provisioning and offline translation in a worker process."""
from __future__ import annotations

import argparse
import atexit
import contextlib
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

PROBE = 'Научное исследование'
PAIRS = {'ru_en': ('ru', 'en', PROBE), 'en_ru': ('en', 'ru', 'Scientific research')}


def translated_language_valid(text, pair):
    if not isinstance(text, str) or not text.strip():
        return False
    cyrillic = bool(re.search('[А-Яа-яЁё]', text))
    return not cyrillic if pair == 'ru_en' else cyrillic


def worker_environment(root=None):
    environment = dict(os.environ)
    home = root or environment.get('PORTFOLIO_TRANSLATION_HOME')
    if home:
        base = Path(home).resolve()
        environment.update(PORTFOLIO_TRANSLATION_HOME=str(base),
                           ARGOS_PACKAGES_DIR=str(base / 'packages'),
                           XDG_DATA_HOME=str(base / 'data'),
                           XDG_CACHE_HOME=str(base / 'cache'),
                           XDG_CONFIG_HOME=str(base / 'config'))
    environment.update(ARGOS_DEVICE_TYPE='cpu', ARGOS_MODEL_PROVIDER='OPENNMT',
                       ARGOS_CHUNK_TYPE='MINISBD', ARGOS_INTER_THREADS='1',
                       ARGOS_INTRA_THREADS='2', PYTHONUNBUFFERED='1', PYTHONUTF8='1')
    return environment


def offline_network(*args, **kwargs):
    raise OSError('Translation workers use installed models only')


def load_translation(allow_download=False, pair='ru_en'):
    source_code, target_code, probe_text = PAIRS[pair]
    if allow_download:
        socket.setdefaulttimeout(20)
    else:
        # Also block lazy SBD downloads. Provisioning warms that model separately.
        socket.socket.connect = offline_network
        socket.create_connection = offline_network
        socket.getaddrinfo = offline_network
    import argostranslate.package as package
    import argostranslate.settings as settings
    import argostranslate.translate as translate

    installed = package.get_installed_packages()
    model = next((p for p in installed if p.from_code == source_code and p.to_code == target_code), None)
    if model is None and allow_download:
        package.update_package_index()
        # Argos recursively retries a missing index; stop here instead.
        if not settings.local_package_index.is_file():
            raise RuntimeError('model_index_unavailable')
        available = package.get_available_packages()
        model = next((p for p in available if p.from_code == source_code and p.to_code == target_code), None)
        if model is not None:
            package.install_from_path(model.download())
    if model is None:
        raise RuntimeError(pair + '_model_unavailable')
    languages = translate.get_installed_languages()
    source = next((p for p in languages if p.code == source_code), None)
    target = next((p for p in languages if p.code == target_code), None)
    translation = source.get_translation(target) if source and target else None
    if translation is None:
        raise RuntimeError(pair + '_pair_unavailable')
    probe = translation.translate(probe_text).strip()
    if probe == probe_text or not translated_language_valid(probe, pair):
        raise RuntimeError('model_validation_failed')
    return translation, {'argos_version': importlib.metadata.version('argostranslate'),
                         'package_version': str(model.package_version), 'pair': pair,
                         'device': 'cpu', 'probe_passed': True}


def worker(allow_download=False, pair='ru_en'):
    protocol = sys.stdout

    def emit(value):
        protocol.write(json.dumps(value, ensure_ascii=False) + '\n')
        protocol.flush()

    try:
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            translation, metadata = load_translation(allow_download, pair)
        emit({'status': 'ready', **metadata})
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) and re.fullmatch('[a-z_]+', str(exc)) else type(exc).__name__
        emit({'status': 'unavailable_' + reason})
        return 2
    if allow_download:
        return 0
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            text = payload.get('text', '')
            if not isinstance(text, str) or len(text) > 6000:
                emit({'status': 'input_too_long'})
                continue
            with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                result = translation.translate(text).strip()
            emit({'status': 'translated', 'text': result})
        except Exception as exc:
            emit({'status': 'translation_failed_' + type(exc).__name__})
    return 0


class BoundedArgosTranslator:
    def __init__(self, *, init_timeout=45, item_timeout=12, total_budget=90,
                 root=None, provision=False, worker_command=None, pair='ru_en'):
        if pair not in PAIRS:
            raise ValueError('unsupported_translation_pair')
        self.pair = pair
        self.status = 'not_initialized'
        self.metadata = {}
        self._attempted = False
        self._ready = False
        self._process = None
        self._responses = queue.Queue()
        self._init_timeout = init_timeout
        self._item_timeout = item_timeout
        self._total_budget = total_budget
        self._deadline = None
        self._root = root
        self._provision = provision
        self._command = worker_command
        atexit.register(self.close)

    def _read_responses(self, stream):
        try:
            for line in stream:
                try:
                    self._responses.put(json.loads(line))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            self._responses.put(None)

    def _receive(self, timeout, timeout_status):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.status = 'translation_budget_exceeded'
            self.close()
            return None
        try:
            result = self._responses.get(timeout=min(timeout, remaining))
        except queue.Empty:
            self.status = timeout_status if remaining > timeout else 'translation_budget_exceeded'
            self.close()
            return None
        if not isinstance(result, dict):
            self.status = 'translation_worker_exited'
            self.close()
            return None
        return result

    def ensure(self):
        if self._attempted:
            return self._ready
        self._attempted = True
        self._deadline = time.monotonic() + self._total_budget
        command = self._command or [sys.executable, '-u', str(Path(__file__).resolve()),
                                    '_provision_worker' if self._provision else '_worker', '--pair', self.pair]
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', env=worker_environment(self._root),
                start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            threading.Thread(target=self._read_responses, args=(self._process.stdout,), daemon=True).start()
        except OSError:
            self.status = 'translation_worker_start_failed'
            return False
        result = self._receive(self._init_timeout, 'model_initialization_timeout')
        if not result:
            return False
        if result.get('status') != 'ready':
            self.status = result.get('status', 'model_initialization_failed')
            self.close()
            return False
        self.metadata = {key: result[key] for key in ('argos_version', 'package_version', 'pair', 'device', 'probe_passed') if key in result}
        if self.metadata.get('pair') not in (None, self.pair):
            self.status = 'translation_model_pair_mismatch'
            self.close()
            return False
        self.status = 'argos_' + self.pair
        self._ready = True
        return True

    def translate(self, text):
        if not self.ensure():
            return ''
        if len(text) > 6000:
            self.status = 'translation_input_too_long'
            return ''
        try:
            self._process.stdin.write(json.dumps({'text': text}, ensure_ascii=False) + '\n')
            self._process.stdin.flush()
        except (OSError, ValueError):
            self.status = 'translation_worker_exited'
            self.close()
            return ''
        result = self._receive(self._item_timeout, 'translation_timeout')
        if not result:
            return ''
        translated = result.get('text', '')
        if result.get('status') != 'translated' or not translated_language_valid(translated, self.pair):
            self.status = result.get('status') if result.get('status') != 'translated' else 'translation_incomplete'
            return ''
        self.status = 'argos_' + self.pair
        return translated

    def close(self):
        self._ready = False
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                if os.name == 'nt':
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == 'nt':
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()
        self._process = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['provision', 'verify', '_worker', '_provision_worker'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--pair', choices=tuple(PAIRS), default='ru_en')
    args = parser.parse_args(argv)
    if args.command.startswith('_'):
        return worker(args.command == '_provision_worker', args.pair)
    timeout = args.timeout or (240 if args.command == 'provision' else 45)
    backend = BoundedArgosTranslator(init_timeout=timeout, total_budget=timeout,
                                     root=args.root, provision=args.command == 'provision', pair=args.pair)
    successful = backend.ensure()
    report = {'attempted_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              'status': 'success' if successful else 'unavailable', 'reason': backend.status,
              'mode': args.command, **backend.metadata}
    backend.close()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if successful else 2


if __name__ == '__main__':
    raise SystemExit(main())
