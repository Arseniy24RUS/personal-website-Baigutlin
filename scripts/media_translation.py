"""Cached optional offline translation; manual translations always win."""
import hashlib
import json
import re
from translation_runtime import BoundedArgosTranslator


def pending_translation_fields(record):
    """Only absent English fields with a usable Russian original may wait."""
    return [field + '_en' for field in ('title', 'description', 'source_name')
            if not str(record.get(field + '_en') or '').strip()
            and re.search('[А-Яа-яЁё]', str(record.get(field + '_ru') or record.get(field) or ''))]


def update_translation_state(record, reason=None):
    fields = pending_translation_fields(record)
    previous = record.get('translation_state') or {}
    if fields:
        record['translation_state'] = {'status': 'pending', 'fields': fields,
                                       'reason': reason or previous.get('reason') or 'translation_unavailable'}
    elif previous:
        record['translation_state'] = {'status': 'complete', 'fields': [], 'reason': None}


class MediaTranslator:
    def __init__(self, path):
        self.path = path
        self.cache = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.status = 'not_needed'
        self._translation = None
        self._runtime = None
        self._attempted = False
        self._changed = False

    def ensure(self):
        if self._attempted:
            return self._translation is not None
        self._attempted = True
        self._runtime = BoundedArgosTranslator()
        successful = self._runtime.ensure()
        self.status = self._runtime.status
        if successful:
            self._translation = self._runtime
        return successful

    def enrich(self, record):
        for field in ('title', 'description', 'source_name'):
            if record.get(field + '_en'):
                continue
            original = record.get(field + '_ru') or record.get(field) or ''
            if not original:
                continue
            if not re.search('[А-Яа-яЁё]', original):
                record[field + '_en'] = original
                continue
            key = hashlib.sha256(('ru:en:' + original).encode()).hexdigest()
            cached = self.cache.get(key)
            if cached:
                record[field + '_en'] = cached['translation']
                record[field + '_en_origin'] = 'cached_argos_ru_en'
                continue
            if not self.ensure():
                continue  # The existing UI falls back to the Russian text.
            try:
                result = self._translation.translate(original).strip()
                if self._runtime is not None:
                    self.status = self._runtime.status
                if result and result != original and not re.search('[А-Яа-яЁё]', result):
                    record[field + '_en'] = result
                    record[field + '_en_origin'] = 'argos_ru_en'
                    self.cache[key] = {'original': original, 'translation': result, 'model': 'ru_en'}
                    self._changed = True
            except Exception as exc:
                self.status = 'translation_failed_' + type(exc).__name__
        update_translation_state(record, self.status)

    def save(self):
        if self._runtime is not None:
            self._runtime.close()
        if self._changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            staged = self.path.with_suffix('.tmp')
            staged.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            staged.replace(self.path)
