"""Runner-only public egress guard: address policy and isolated rule lifecycle."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/it_public_network.sh'


class NetworkGuardContractTests(unittest.TestCase):
    def test_nonpublic_ipv4_and_ipv6_ranges_are_blocked(self):
        text = SCRIPT.read_text(encoding='utf-8')
        def ranges(name):
            body = re.search(rf'{name}=\((.*?)\)', text, re.S).group(1)
            return [ipaddress.ip_network(value) for value in body.split()]
        ipv4, ipv6 = ranges('IPV4_DENY'), ranges('IPV6_DENY')
        for value in ('0.0.0.0', '10.0.0.1', '100.100.100.200', '127.0.0.1',
                      '169.254.169.254', '168.63.129.16', '172.16.1.1', '192.168.0.1',
                      '192.0.2.1', '198.18.0.1', '224.0.0.1', '255.255.255.255'):
            with self.subTest(value=value):
                self.assertTrue(any(ipaddress.ip_address(value) in network for network in ipv4))
        self.assertIn('-d 2000::/3 -j RETURN', text)
        self.assertIn('firewall ip6tables -A "$CHAIN" -j REJECT', text)
        global_ipv6 = ipaddress.ip_network('2000::/3')
        for value in ('::1', '::ffff:127.0.0.1', 'fc00::1', 'fe80::1', 'ff02::1',
                      '64:ff9b::a00:1', '2001:db8::1', '2002:7f00:1::1', '3fff::1'):
            address = ipaddress.ip_address(value)
            with self.subTest(value=value):
                self.assertTrue(address not in global_ipv6 or any(address in network for network in ipv6))
        for value in ('1.1.1.1', '140.82.112.3'):
            self.assertFalse(any(ipaddress.ip_address(value) in network for network in ipv4))
        for value in ('2606:4700:4700::1111', '2001:4860:4860::8888'):
            self.assertFalse(any(ipaddress.ip_address(value) in network for network in ipv6))

    def test_workflow_limits_guard_to_collection_and_always_cleans_up(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/refresh-it-resources.yml').read_text(encoding='utf-8'))
        steps = workflow['jobs']['discover']['steps']
        find = lambda fragment: next(i for i, step in enumerate(steps) if fragment in step.get('run', ''))
        start, collect, stop = find('it_public_network.sh start'), find('refresh_pipeline.py collect'), find('it_public_network.sh stop')
        self.assertLess(find('translation_runtime.py provision'), start)
        self.assertLess(start, collect)
        self.assertLess(collect, stop)
        self.assertLess(stop, find('playwright test'))
        self.assertEqual(steps[stop]['if'], 'always()')
        self.assertEqual(steps[collect]['env']['IT_PUBLIC_NETWORK_REQUIRED'], '1')
        self.assertIn('IT_GITHUB_TOKEN', steps[collect]['env'])
        self.assertNotIn('GH_TOKEN', steps[collect]['env'])
        script = SCRIPT.read_text(encoding='utf-8')
        self.assertNotIn('-F OUTPUT', script)
        self.assertNotIn('-P OUTPUT', script)
        self.assertIn('-F "$CHAIN"', script)
        self.assertIn('-X "$CHAIN"', script)
        self.assertIn('trap stop_guard EXIT', script)
        self.assertIn('--dport 53 -j RETURN', script)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'iptables shell lifecycle is exercised on the Ubuntu runner')
    def test_actual_shell_lifecycle_uses_only_dedicated_chains(self):
        # Execute the real shell script against deterministic fake command-line
        # iptables tools, never touch the test host's firewall or require sudo.
        fake = '''#!/usr/bin/env python
import json, os, pathlib, sys
path = pathlib.Path(os.environ['IT_FIREWALL_TEST_STATE'])
state = json.loads(path.read_text())
family = pathlib.Path(sys.argv[0]).name
tables = state[family]
args = sys.argv[1:]
if args[:1] == ['-w']: args = args[2:]
action, chain, *rule = args
state['commands'].append([family, *args])
code = 0
if action == '-N':
    if chain in tables: code = 1
    else: tables[chain] = []
elif action == '-S': code = 0 if chain in tables else 1
elif action == '-C': code = 0 if rule in tables.get(chain, []) else 1
elif action == '-A': tables[chain].append(rule)
elif action == '-I': tables[chain].insert(int(rule[0]) - 1, rule[1:])
elif action == '-D': tables[chain].remove(rule)
elif action == '-F': tables[chain] = []
elif action == '-X': del tables[chain]
else: raise ValueError(action)
path.write_text(json.dumps(state))
sys.exit(code)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'bin'
            binary.mkdir()
            state = root / 'rules.json'
            seed = {'OUTPUT': [['-j', 'UNRELATED']], 'UNRELATED': [['-j', 'RETURN']]}
            state.write_text(json.dumps({'iptables': seed, 'ip6tables': seed, 'commands': []}))
            for name in ('iptables', 'ip6tables'):
                path = binary / name
                path.write_text(fake)
                path.chmod(0o755)
            sudo = binary / 'sudo'
            sudo.write_text('#!/bin/sh\nif test "$1" = -n; then shift; fi\nexec "$@"\n')
            sudo.chmod(0o755)
            environment = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                           'IT_FIREWALL_TEST_STATE': str(state)}
            def execute(action, check=True):
                return subprocess.run(['bash', str(SCRIPT), action], env=environment, capture_output=True, text=True, check=check)
            execute('start')
            execute('check')
            active = json.loads(state.read_text())
            for family in ('iptables', 'ip6tables'):
                self.assertEqual(active[family]['OUTPUT'][0], ['-j', 'PORTFOLIO_IT_PUBLIC'])
                rules = active[family]['PORTFOLIO_IT_PUBLIC']
                for rule in rules:
                    if '--dport' in rule:
                        self.assertIn(rule[rule.index('-p') + 1], ('tcp', 'udp'))
                        self.assertEqual(rule[rule.index('--dport') + 1], '53')
                    if '--sport' in rule:
                        self.assertEqual(rule[rule.index('--sport') + 1], '53')
                        self.assertEqual(rule[rule.index('--ctstate') + 1], 'ESTABLISHED')
            self.assertNotEqual(execute('start', check=False).returncode, 0)
            execute('check')
            execute('stop')
            execute('stop')
            cleaned = json.loads(state.read_text())
            self.assertEqual(cleaned['iptables'], seed)
            self.assertEqual(cleaned['ip6tables'], seed)
            self.assertNotEqual(execute('check', check=False).returncode, 0)
            mutations = [row for row in cleaned['commands'] if row[1] in ('-F', '-X')]
            self.assertTrue(mutations)
            self.assertTrue(all(row[2] == 'PORTFOLIO_IT_PUBLIC' for row in mutations))


if __name__ == '__main__':
    unittest.main()
