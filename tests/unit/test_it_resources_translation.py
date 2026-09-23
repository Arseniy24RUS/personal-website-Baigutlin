import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from it_translation import ITTranslator
from translation_runtime import BoundedArgosTranslator, translated_language_valid
from harvest_it_resources import GitHubClient, ITFailure


class ITTranslationTests(unittest.TestCase):
    def test_native_bilingual_fields_never_start_translation(self):
        with tempfile.TemporaryDirectory() as temporary:
            factory = Mock(side_effect=AssertionError('native text must win'))
            row = {'title_ru':'GIR','title_en':'GIR','description_ru':'Описание проекта','description_en':'Project description'}
            original = dict(row)
            self.assertTrue(ITTranslator(Path(temporary)/'cache.json', factory).enrich(row))
            self.assertEqual(row, original)

    def test_each_missing_direction_has_separate_verified_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            instances = []
            def factory(**kw):
                backend = Mock(metadata={'pair':kw['pair'],'probe_passed':True},status='translated')
                backend.translate.return_value = 'Русское описание' if kw['pair']=='en_ru' else 'English description'
                instances.append(backend)
                return backend
            path=Path(temporary)/'cache.json'
            translator=ITTranslator(path,factory)
            one={'title_en':'Project Title','description_en':'Public information explorer'}
            two={'title_ru':'Название проекта','description_ru':'Исследование открытых данных'}
            self.assertTrue(translator.enrich(one))
            translator.flush()
            self.assertTrue(translator.enrich(two))
            translator.save()
            self.assertEqual(len(instances),2)
            self.assertEqual({r['pair'] for r in json.loads(path.read_text(encoding='utf-8')).values()},{'ru_en','en_ru'})
            factory2=Mock(side_effect=AssertionError('should reuse cache'))
            repeat={'title_en':'Project Title','description_en':'Public information explorer'}
            self.assertTrue(ITTranslator(path,factory2).enrich(repeat))

    def test_failed_reverse_translation_retains_native_without_false_russian(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend=Mock(metadata={},status='translation_timeout')
            backend.translate.return_value='Still English'
            row={'title_en':'Project Title','description_en':'Native explanation'}
            self.assertFalse(ITTranslator(Path(temporary)/'cache.json', lambda **kw:backend).enrich(row))
            self.assertEqual(row,{'title_en':'Project Title','description_en':'Native explanation'})

    def test_reverse_worker_validates_output_language_and_mismatch(self):
        self.assertTrue(translated_language_valid('Научное исследование','en_ru'))
        self.assertFalse(translated_language_valid('Scientific research','en_ru'))
        worker="import json,sys; print(json.dumps({'status':'ready','pair':'en_ru','probe_passed':True}),flush=True)\nfor line in sys.stdin: print(json.dumps({'status':'translated','text':'Научное исследование'}),flush=True)"
        runtime=BoundedArgosTranslator(pair='en_ru',worker_command=[sys.executable,'-u','-c',worker],init_timeout=4)
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.translate('Scientific research'),'Научное исследование')
        self.assertEqual(runtime.status,'argos_en_ru')
        wrong=BoundedArgosTranslator(pair='ru_en',worker_command=[sys.executable,'-u','-c',worker],init_timeout=4)
        self.addCleanup(wrong.close)
        self.assertFalse(wrong.ensure())
        self.assertEqual(wrong.status,'translation_model_pair_mismatch')


def response(status, payload=None, headers=None):
    value=Mock(status_code=status,headers=headers or {})
    value.json.return_value=payload
    return value


class ITGitHubTests(unittest.TestCase):
    def test_all_pages_and_fixed_token_host(self):
        session=Mock()
        session.get.side_effect=[response(200,[{'id':n} for n in range(1,101)]),response(200,[{'id':101}])]
        batches=list(GitHubClient('test-token',session).repositories('Owner'))
        self.assertEqual(sum(map(len,batches)),101)
        self.assertEqual([c.kwargs['params']['page'] for c in session.get.call_args_list],[1,2])
        for call in session.get.call_args_list:
            self.assertTrue(call.args[0].startswith('https://api.github.com/users/'))
            self.assertEqual(call.kwargs['headers']['Authorization'],'Bearer test-token')
            self.assertFalse(call.kwargs['allow_redirects'])

    def test_retry_after_is_honored_or_deferred_without_early_retry(self):
        for delay, expected in [('2',2),('60',1)]:
            with self.subTest(delay=delay):
                session=Mock()
                session.get.side_effect=[response(429,headers={'Retry-After':delay}),response(200,[])]
                clock=[0]
                client=GitHubClient(session=session,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
                if delay=='60':
                    with self.assertRaises(ITFailure) as error:
                        client.get('/users/Owner/repos')
                    self.assertEqual(error.exception.reason,'github_rate_limited')
                    self.assertEqual(clock[0],0)
                else:
                    self.assertEqual(client.get('/users/Owner/repos'),[])
                    self.assertEqual(clock[0],2)
                self.assertEqual(session.get.call_count,expected)

    def test_api_redirect_never_forwards_key_or_silently_follows(self):
        session=Mock()
        session.get.return_value=response(302,headers={'Location':'https://other.test/'})
        with self.assertRaises(ITFailure):
            GitHubClient('secret-fixture',session).get('/repos/Owner/Test/readme')
        self.assertEqual(session.get.call_count,1)

    def test_pages_custom_domain_and_access_denied_fallback(self):
        session=Mock()
        session.get.side_effect=[response(200,{'html_url':'https://custom.test/'}),response(403)]
        client=GitHubClient(session=session)
        repo={'full_name':'Owner/Test','has_pages':True}
        self.assertEqual(client.pages(repo),'https://custom.test/')
        self.assertIsNone(client.pages(repo))


if __name__=='__main__':
    unittest.main()
