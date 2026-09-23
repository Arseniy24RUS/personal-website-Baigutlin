"""Bidirectional, cached translation of new IT cards; native text always wins."""
from __future__ import annotations

import hashlib
from it_resources import atomic_json, read_json
from translation_runtime import BoundedArgosTranslator, translated_language_valid


class ITTranslator:
    def __init__(self, path, runtime_factory=BoundedArgosTranslator):
        self.path = path
        self.cache = read_json(path, {})
        self.factory = runtime_factory
        self.runtimes = {}
        self.status = 'not_needed'

    def enrich(self, row):
        for field in ('title', 'description'):
            for source, target in (('ru', 'en'), ('en', 'ru')):
                if row.get(field + '_' + target) or not row.get(field + '_' + source):
                    continue
                original = row[field + '_' + source]
                pair = source + '_' + target
                key = hashlib.sha256((pair + ':' + original).encode()).hexdigest()
                cached = self.cache.get(key, {})
                translated = cached.get('translation', '')
                if not translated_language_valid(translated, pair):
                    runtime = self.runtimes.setdefault(pair, None)
                    if runtime is None:
                        runtime = self.runtimes[pair] = self.factory(pair=pair, total_budget=180)
                    translated = runtime.translate(original)
                    self.status = runtime.status
                    if translated_language_valid(translated, pair):
                        self.cache[key] = {'translation': translated, 'pair': pair,
                                           'model': runtime.metadata}
                if translated_language_valid(translated, pair):
                    row[field + '_' + target] = translated
        complete = all(row.get(field + '_' + lang) for field in ('title', 'description') for lang in ('ru', 'en'))
        return complete

    def save(self):
        for runtime in self.runtimes.values():
            if runtime:
                runtime.close()
        self.flush()

    def flush(self):
        if self.cache:
            atomic_json(self.path, self.cache)
