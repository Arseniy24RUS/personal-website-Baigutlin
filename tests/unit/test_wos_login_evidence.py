"""Credential-free progress of ordinary WoS/ORCID login, with no browser/network."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth

USERNAME = 'fixture@example.invalid'
PASSWORD = 'synthetic-password-private'
PROFILE = 'https://www.webofscience.com/wos/author/record/FIXTURE-1?SID=synthetic-url-private'


class Field:
    def __init__(self):
        self.value = ''

    def fill(self, value):
        self.value = value

    def input_value(self):
        return self.value


class Flow:
    def __init__(self, fail_at=None, *, direct_session=False):
        self.fail_at = fail_at
        self.direct_session = direct_session
        self.phase = 'initial'
        self.pages = []
        self.url = 'about:blank'
        self.calls = []
        self.user, self.secret = Field(), Field()
        self.listeners = {}
        self.context = self
        self.main_frame = SimpleNamespace(page=self, parent_frame=None)

    def new_page(self):
        self.pages.append(self)
        return self

    def is_closed(self):
        return False

    def goto(self, url, **kwargs):
        if url == PROFILE:
            if self.phase == 'initial':
                self.calls.append('initial_profile')
                if self.fail_at == 'initial_profile':
                    raise RuntimeError('synthetic-transport-private')
                self.phase, self.url = 'profile_entry', url
                return
            self.calls.append('profile')
            self.phase, self.url = 'profile', url
            return
        self.calls.append('homepage')
        if self.fail_at == 'homepage':
            raise RuntimeError('synthetic-transport-private')
        self.phase, self.url = 'homepage', 'https://www.webofscience.com/'

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners.get(event, []).remove(callback)

    def guard(self, page, **kwargs):
        if self.phase == self.fail_at:
            raise auth.AuthFailure('human_verification_required')

    def visible(self, page, selectors):
        if self.phase != 'orcid':
            return None
        if selectors[0] == '#username-input':
            return self.user
        if selectors[0] == '#password':
            return self.secret
        if selectors[0] == 'button#signin-button[type="submit"]':
            return self
        return None

    def click_named(self, page, pattern):
        if self.phase == 'profile_entry':
            self.calls.append('signin')
            self.phase, self.url = 'clarivate', 'https://signin.clarivate.com/'
            return True
        return False

    def choose_orcid(self, page, host):
        self.calls.append('orcid_selection')
        if self.fail_at == 'delayed_popup':
            self.phase, self.url = 'delayed_popup', 'https://www.webofscience.com/'
        else:
            self.phase, self.url = 'orcid', 'https://orcid.org/signin?token=synthetic-url-private'
        return True

    def click(self, **kwargs):
        self.calls.append('submit')
        if self.fail_at == 'submit':
            raise RuntimeError('synthetic-submit-private')
        response = SimpleNamespace(
            url='https://orcid.org/signin/auth.json', request=SimpleNamespace(method='POST', frame=self.main_frame), status=200,
            json=lambda: {'success': True, 'email': USERNAME, 'token': 'synthetic-response-private'},
        )
        for callback in list(self.listeners.get('response', [])):
            callback(response)
        self.phase = 'after_submit' if self.fail_at == 'after_submit' else 'wos_return'
        self.url = 'https://orcid.org/signin' if self.phase == 'after_submit' else 'https://www.webofscience.com/'

    def authenticated(self, page):
        return self.phase == 'wos_return' or (self.direct_session and self.phase == 'profile_entry')


class LoginProgressTests(unittest.TestCase):
    def login(self, flow):
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': USERNAME, 'WOS_ORCID_PASSWORD': PASSWORD}), \
                patch.object(auth, '_wait_wos_login_navigation'), \
                patch.object(auth, 'prepare_wos_profile_login'), \
                patch.object(auth, 'safe_browser_diagnostics', return_value=[]), \
                patch.object(auth, 'assert_no_challenge', side_effect=flow.guard), \
                patch.object(auth, 'visible', side_effect=flow.visible), \
                patch.object(auth, 'click_named', side_effect=flow.click_named), \
                patch.object(auth, 'choose_orcid_signin', side_effect=flow.choose_orcid), \
                patch.object(auth, 'wos_authenticated', side_effect=flow.authenticated):
            return auth.login_wos(flow, PROFILE)

    def failed(self, flow):
        with self.assertRaises(auth.AuthFailure) as caught:
            self.login(flow)
        return caught.exception.authentication_evidence

    def test_initial_profile_failure_does_not_claim_homepage_or_orcid_attempt(self):
        evidence = self.failed(Flow('initial_profile'))
        self.assertEqual(evidence['stage'], 'initial_profile')
        self.assertTrue(evidence['initial_profile_requested'])
        self.assertFalse(evidence['initial_profile_loaded'])
        self.assertFalse(evidence['homepage_requested'])
        self.assertFalse(evidence['homepage_loaded'])
        self.assertFalse(evidence['orcid_selected'])
        self.assertFalse(evidence['submit_clicked'])

    def test_orcid_challenge_proves_selection_but_not_form_submission(self):
        evidence = self.failed(Flow('orcid'))
        self.assertEqual(evidence['stage'], 'orcid_page')
        for field in ('initial_profile_loaded', 'signin_clicked', 'clarivate_observed', 'orcid_selected', 'orcid_page_observed'):
            self.assertTrue(evidence[field])
        self.assertFalse(evidence['orcid_form_observed'])
        self.assertFalse(evidence['submit_clicked'])

    def test_selected_orcid_with_delayed_popup_is_not_reported_as_wos_return(self):
        evidence = self.failed(Flow('delayed_popup'))
        self.assertTrue(evidence['orcid_selected'])
        self.assertFalse(evidence['orcid_page_observed'])
        self.assertFalse(evidence['wos_return_observed'])
        self.assertFalse(evidence['submit_clicked'])

    def test_submit_interaction_failure_is_not_marked_submitted(self):
        evidence = self.failed(Flow('submit'))
        self.assertEqual(evidence['stage'], 'orcid_submit')
        self.assertTrue(evidence['orcid_form_observed'])
        self.assertTrue(evidence['input_matches_configured'])
        self.assertFalse(evidence['submit_clicked'])

    def test_challenge_after_submit_preserves_submission_and_safe_response(self):
        evidence = self.failed(Flow('after_submit'))
        self.assertEqual(evidence['stage'], 'orcid_response')
        self.assertTrue(evidence['submit_clicked'])
        self.assertTrue(evidence['response_observed'])
        self.assertTrue(evidence['success'])
        self.assertEqual(evidence['http_status'], 200)
        self.assertFalse(evidence['wos_return_observed'])
        for secret in (USERNAME, PASSWORD, 'synthetic-url-private', 'synthetic-response-private'):
            self.assertNotIn(secret, json.dumps(evidence))

    def test_wos_return_challenge_keeps_orcid_proof_without_claiming_verified_profile(self):
        evidence = self.failed(Flow('wos_return'))
        self.assertEqual(evidence['stage'], 'wos_return')
        self.assertTrue(evidence['submit_clicked'])
        self.assertTrue(evidence['wos_return_observed'])
        self.assertFalse(evidence['wos_session_confirmed'])
        self.assertFalse(evidence['profile_requested'])

    def test_success_carries_safe_progress_for_later_profile_validation(self):
        flow = Flow()
        page = self.login(flow)
        self.assertIs(page, flow)
        self.assertEqual(flow.calls, ['initial_profile', 'signin', 'orcid_selection', 'submit', 'profile'])
        evidence = page._wos_login_evidence
        self.assertEqual(evidence['stage'], 'complete')
        for field in ('submit_clicked', 'wos_return_observed', 'wos_session_confirmed', 'profile_requested', 'profile_loaded'):
            self.assertTrue(evidence[field])
        self.assertNotIn('target_verified', evidence)
        self.assertNotIn('synthetic', json.dumps(evidence))

    def test_direct_existing_session_is_not_mislabeled_as_orcid_login(self):
        page = self.login(Flow(direct_session=True))
        evidence = page._wos_login_evidence
        self.assertTrue(evidence['wos_session_confirmed'])
        self.assertTrue(evidence['returned_profile_reused'])
        self.assertFalse(evidence['profile_requested'])
        for field in ('signin_clicked', 'orcid_selected', 'orcid_page_observed', 'submit_clicked', 'wos_return_observed'):
            self.assertFalse(evidence[field])

    def test_only_local_progress_can_claim_completed_stages_on_failure(self):
        forged = auth.AuthFailure('human_verification_required', authentication_evidence={
            'stage': 'complete', 'submit_clicked': True, 'url': PROFILE, 'password': PASSWORD,
            'response_observed': True, 'http_status': 401,
        })
        with patch.object(auth, '_login_wos', side_effect=forged), patch.object(auth, 'safe_browser_diagnostics', return_value=[]):
            with self.assertRaises(auth.AuthFailure) as caught:
                auth.login_wos(Flow(), PROFILE)
        evidence = caught.exception.authentication_evidence
        self.assertEqual(evidence['stage'], 'initialization')
        self.assertFalse(evidence['submit_clicked'])
        self.assertTrue(evidence['response_observed'])
        self.assertEqual(evidence['http_status'], 401)
        self.assertNotIn('synthetic', json.dumps(evidence))


class EvidenceSchemaTests(unittest.TestCase):
    def test_allowlist_rejects_raw_data_fake_stage_and_non_boolean_flags(self):
        value = {'stage': 'https://private.invalid/session', 'homepage_loaded': 'true',
                 'submit_clicked': 1, 'orcid_selected': True, 'http_status': True,
                 'reason': 'synthetic-private-reason', 'url': PROFILE, 'password': PASSWORD,
                 'response': {'token': 'synthetic-private-response'}}
        self.assertEqual(auth.safe_wos_login_evidence(value), {'orcid_selected': True})

    def test_response_markers_and_fixed_stage_survive_without_raw_payload(self):
        self.assertEqual(auth.safe_wos_login_evidence({
            'stage': 'orcid_response', 'http_status': 429, 'reason': 'orcid_auth_http_429',
            'verificationCodeRequired': True, 'response_observed': True, 'body': 'synthetic-private',
        }), {'stage': 'orcid_response', 'http_status': 429, 'reason': 'orcid_auth_http_429',
             'verificationCodeRequired': True, 'response_observed': True})
        self.assertEqual(auth.safe_wos_login_evidence(None), {})


if __name__ == '__main__':
    unittest.main()
