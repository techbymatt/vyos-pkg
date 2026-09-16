"""Integration tests with fake package tools; run with unittest discovery."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('install_packages.sh').resolve()
APT_OPTIONS = [
    '-o', 'APT::Sandbox::User=root',
    '-o', 'Dpkg::Options::=--force-confdef',
    '-o', 'Dpkg::Options::=--force-confold',
    '--no-install-recommends', '--allow-downgrades', '--reinstall', '--yes',
]
TARGETS = ['alpha=1:2.0-3', 'beta=4.5-6']

# Branching lives in external dependency fixtures, not in test expectations.
FAKE_TOOL = r'''
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps([tool, args]) + '\n')
if tool == 'sudo':
    os.execvp(args[0], args)
elif tool == 'dpkg-deb':
    assert args[0] == '--field'
    metadata = json.loads(Path(args[1]).read_text())
    print(metadata[args[2]])
elif tool == 'apt-get':
    if '--simulate' in args:
        print('simulation stdout')
        print('simulation diagnostic', file=sys.stderr)
        sys.exit(int(os.environ['SIMULATION_STATUS']))
    print('real install')
    sys.exit(int(os.environ['INSTALL_STATUS']))
'''


class InstallPackagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls.jsonl'
        self.log.write_text('')
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        CALL_LOG=str(self.log), TMPDIR=str(self.tmp),
                        SIMULATION_STATUS='0', INSTALL_STATUS='0')
        for name in ('sudo', 'apt-get', 'dpkg-deb'):
            executable = self.bin / name
            executable.write_text('#!' + sys.executable + '\n' + FAKE_TOOL)
            executable.chmod(0o755)
        self.alpha = self.deb('first package.deb', 'alpha', '1:2.0-3')
        self.beta = self.deb('second.deb', 'beta', '4.5-6')
        self.excluded = self.deb('third.deb', 'strongswan-charon', '5.0')
        self.excluded_debug = self.deb('fourth.deb', 'strongswan-charon-dbgsym', '5.0')

    def deb(self, filename, package, version):
        path = self.root / filename
        path.write_text(json.dumps({'Package': package, 'Version': version}))
        return str(path)

    def run_script(self, *paths):
        return subprocess.run([shutil.which('bash'), str(SCRIPT), *paths],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def calls(self, tool):
        records = map(json.loads, self.log.read_text().splitlines())
        return [args for name, args in records if name == tool]

    def test_coinstallable_packages_use_one_real_batch(self):
        result = self.run_script(self.alpha, self.beta)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [APT_OPTIONS + ['--simulate', 'install'] + TARGETS,
                    APT_OPTIONS + ['install'] + TARGETS]
        self.assertEqual(self.calls('apt-get'), commands)
        self.assertEqual(self.calls('sudo'), [['apt-get'] + args for args in commands])
        self.assertEqual(self.calls('dpkg-deb'), [
            ['--field', self.alpha, 'Package'], ['--field', self.alpha, 'Version'],
            ['--field', self.beta, 'Package'], ['--field', self.beta, 'Version'],
        ])
        self.assertNotIn('WARNING', result.stderr)
        self.assertRegex(result.stdout, r'APT simulation: elapsed \d+ seconds \(status 0\)')
        self.assertRegex(result.stderr, r'APT batch install: elapsed \d+ seconds \(status 0\)')
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_simulation_conflict_falls_back_in_input_order(self):
        self.env['SIMULATION_STATUS'] = '100'
        result = self.run_script(self.alpha, self.beta)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls('apt-get'), [
            APT_OPTIONS + ['--simulate', 'install'] + TARGETS,
            APT_OPTIONS + ['install', TARGETS[0]],
            APT_OPTIONS + ['install', TARGETS[1]],
        ])
        self.assertIn('WARNING: APT simulation failed; falling back to sequential installs.', result.stderr)
        self.assertIn('simulation stdout', result.stderr)
        self.assertIn('simulation diagnostic', result.stderr)
        self.assertRegex(result.stderr, r'APT simulation: elapsed \d+ seconds \(status 100\)')
        self.assertRegex(result.stderr, r'APT install alpha=1:2.0-3: elapsed \d+ seconds')
        self.assertRegex(result.stderr, r'APT install beta=4.5-6: elapsed \d+ seconds')
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_real_batch_failure_is_not_retried(self):
        self.env['INSTALL_STATUS'] = '42'
        result = self.run_script(self.alpha, self.beta)
        self.assertEqual(result.returncode, 42)
        self.assertEqual(self.calls('apt-get'), [
            APT_OPTIONS + ['--simulate', 'install'] + TARGETS,
            APT_OPTIONS + ['install'] + TARGETS,
        ])
        self.assertNotIn('falling back', result.stderr)
        self.assertRegex(result.stderr, r'APT batch install: elapsed \d+ seconds \(status 42\)')
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_sequential_failure_stops_without_retry(self):
        self.env.update(SIMULATION_STATUS='100', INSTALL_STATUS='42')
        result = self.run_script(self.alpha, self.beta)
        self.assertEqual(result.returncode, 42)
        self.assertEqual(self.calls('apt-get'), [
            APT_OPTIONS + ['--simulate', 'install'] + TARGETS,
            APT_OPTIONS + ['install', TARGETS[0]],
        ])

    def test_excluded_packages_do_not_reach_apt(self):
        result = self.run_script(self.excluded, self.alpha, self.excluded_debug)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls('apt-get'), [
            APT_OPTIONS + ['--simulate', 'install', TARGETS[0]],
            APT_OPTIONS + ['install', TARGETS[0]],
        ])

    def test_empty_target_set_fails_before_apt(self):
        result = self.run_script(self.excluded, self.excluded_debug)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No install targets remain', result.stderr)
        self.assertEqual(self.calls('sudo'), [])
        self.assertEqual(self.calls('apt-get'), [])

    def test_empty_arguments_fail_before_package_tools(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Supply at least one .deb path', result.stderr)
        self.assertEqual(self.log.read_text(), '')


if __name__ == '__main__':
    unittest.main()
