"""Metadata-only exports of profile readiness and challenge observations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import browser_sessions as sessions


class ObservationExportTests(unittest.TestCase):
    def export(self, report):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source'
            destination = Path(temporary) / 'export'
            source.mkdir()
            (source / 'wos.json').write_text(json.dumps(report), encoding='utf-8')
            sessions.export_diagnostics(source, destination)
            return json.loads((destination / 'wos.json').read_text(encoding='utf-8'))

    def sample(self, **changes):
        return {
            'category': 'passive', 'ready_state': 'complete', 'elapsed_seconds': 5.25,
            'frame_dom_observed': 1, 'checkbox_present': 0, 'checkbox_visible': 0,
            'active_challenge_controls': 0, 'observation_incomplete': None,
            'frame_text_length': 0, 'frame_tag_count': 10, 'frame_button_count': 0,
            'frame_role_button_count': 0, 'frame_input_count': 0, 'frame_canvas_count': None,
            'hcaptcha_frame_count': 1, 'recaptcha_frame_count': 0, 'other_frame_count': 0,
            **changes,
        }

    def test_success_and_failure_traces_preserve_exact_safe_fields(self):
        for key, settling in [('profile_entry_observation', 'cleared'), ('verification_evidence', 'timed_out')]:
            with self.subTest(key=key):
                report = {key: {'settling': settling, 'elapsed_seconds': 60.005,
                               'observation_budget_seconds': 60, 'observation_timeline': [self.sample()]}}
                self.assertEqual(self.export(report), report)

    def test_marker_and_trigger_use_fixed_identifiers_only(self):
        report = {'verification_evidence': {
            'trigger': 'page_marker', 'marker_ids': ['verify_you_are_human', 'private-token'],
            'frame': {'provider_host': 'private.example', 'text': 'private-body'},
            'headers': {'Authorization': 'private-header'}, 'url': 'https://private.example/',
        }}
        self.assertEqual(self.export(report), {'verification_evidence': {
            'trigger': 'page_marker', 'marker_ids': ['verify_you_are_human'],
        }})

    def test_read_failures_export_only_fixed_error_and_phase_identifiers(self):
        for error_type in ('timeout', 'navigation', 'closed', 'unexpected'):
            with self.subTest(error_type=error_type):
                report = {'verification_evidence': {
                    'trigger': 'observation_incomplete', 'error_type': error_type,
                    'read_phase': 'page_text',
                    'observation_timeline': [self.sample(
                        category='incomplete', error_type=error_type, read_phase='frame_visibility')],
                }}
                self.assertEqual(self.export(report), report)
        for value in ('private-token', 'https://private.example/?SID=secret', ['timeout'], None):
            with self.subTest(value=value):
                result = self.export({'verification_evidence': {
                    'error_type': value, 'read_phase': value, 'error_message': 'private-text',
                    'observation_timeline': [{'error_type': value, 'read_phase': value,
                                              'exception': 'private-exception'}],
                }})
                self.assertEqual(result, {'verification_evidence': {'observation_timeline': []}})

    def test_rejects_secret_strings_nan_string_counts_and_invalid_flags(self):
        report = {'profile_entry_observation': {
            'settling': 'private-token', 'elapsed_seconds': float('nan'),
            'observation_budget_seconds': float('inf'),
            'observation_timeline': [self.sample(
                category='private-token', ready_state='https://private.example/',
                elapsed_seconds=-1, frame_text_length='private-body', frame_tag_count='12',
                frame_button_count=True, frame_input_count=-1, frame_role_button_count=float('nan'),
                frame_canvas_count=1000001, frame_dom_observed=True, checkbox_present=2,
                checkbox_visible='0', active_challenge_controls=float('inf'),
            )],
            'cookies': [{'value': 'private-cookie'}], 'storage_state': 'private-state',
        }}
        expected_sample = {'observation_incomplete': None, 'hcaptcha_frame_count': 1,
                           'recaptcha_frame_count': 0, 'other_frame_count': 0}
        result = self.export(report)
        self.assertEqual(result, {'profile_entry_observation': {'observation_timeline': [expected_sample]}})
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('NaN', json.dumps(result))

    def test_history_is_bounded_and_preserves_terminal_sample(self):
        rows = [self.sample(elapsed_seconds=index) for index in range(40)]
        rows[-1]['category'] = 'timed_out'
        original = copy.deepcopy(rows)
        result = self.export({'verification_evidence': {'observation_timeline': rows}})
        timeline = result['verification_evidence']['observation_timeline']
        self.assertEqual(len(timeline), 16)
        self.assertEqual([row['elapsed_seconds'] for row in timeline], list(range(15)) + [39])
        self.assertEqual(timeline[-1]['category'], 'timed_out')
        self.assertEqual(rows, original)

    def test_arbitrary_shapes_and_nested_payloads_are_discarded(self):
        result = self.export({'verification_evidence': {
            'trigger': {'value': 'private-token'}, 'marker_ids': [{'value': 'private-token'}],
            'observation_timeline': ['private-token', None, {'category': ['private-token']},
                                     {'frame_text_length': {'value': 'private-token'}}],
        }, 'profile_entry_observation': 'private-token'})
        self.assertEqual(result, {'verification_evidence': {'marker_ids': [], 'observation_timeline': []},
                                  'profile_entry_observation': {}})


if __name__ == '__main__':
    unittest.main()
