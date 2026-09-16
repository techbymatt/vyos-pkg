"""Exercise the planner's jq filter with fixed cache decisions."""

import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


FILTER = Path(__file__).with_name('publish_matrix.jq')
REVISION = 'a' * 40
NAMESPACE = 'b' * 64


class PublishMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.keys = Path(temporary.name) / 'keys'
        self.keys.write_text('')

    def matrices(self, rows: list[str], changed: str = 'true') -> dict:
        result = subprocess.run(
            ['jq', '-rRs', '--rawfile', 'cached_keys', str(self.keys),
             '--arg', 'namespace', NAMESPACE, '--arg', 'run', '123-1',
             '--arg', 'changed', changed, '-f', str(FILTER)],
            input='\n'.join(rows) + '\n', text=True, capture_output=True, check=True)
        return dict((name, json.loads(value)) for name, value in
                    (line.split('=', 1) for line in result.stdout.splitlines()))

    def test_misses_keep_build_groups_architectures_and_dependencies(self) -> None:
        result = self.matrices([
            f'build\tmiss\tfrr\tamd64\t{REVISION}\t',
            f'build-extra\tmiss\thvinfo\tarm64\t{REVISION}\tgnat gprbuild'])
        self.assertEqual(result['restore-matrix']['include'], [])
        self.assertEqual(result['build-matrix']['include'], [{
            'package': 'frr', 'arch': 'amd64', 'runner_label': 'ubuntu-24.04',
            'commit': REVISION, 'group': 'build',
            'cache_key': f'cache-v2-frr-amd64-{REVISION}-{NAMESPACE}-123-1'}])
        self.assertEqual(result['build-extra-matrix']['include'][0]['deps'], 'gnat gprbuild')
        self.assertEqual(result['build-extra-matrix']['include'][0]['runner_label'], 'ubuntu-24.04-arm')

    def test_hits_select_newest_matching_key(self) -> None:
        prefix = f'cache-v2-frr-amd64-{REVISION}-{NAMESPACE}-'
        self.keys.write_text(f'unrelated\n{prefix}122-1\n{prefix}121-1\n')
        result = self.matrices([f'build\thit\tfrr\tamd64\t{REVISION}\t'])
        self.assertEqual(result['build-matrix']['include'], [])
        self.assertEqual(result['restore-matrix']['include'][0]['entries'][0]['cache_key'], prefix + '122-1')

    def test_forced_miss_uses_new_key_despite_existing_cache(self) -> None:
        prefix = f'cache-v2-frr-amd64-{REVISION}-{NAMESPACE}-'
        self.keys.write_text(prefix + '122-1\n')
        result = self.matrices([f'build\tmiss\tfrr\tamd64\t{REVISION}\t'])
        self.assertEqual(result['build-matrix']['include'][0]['cache_key'], prefix + '123-1')

    def test_unchanged_inputs_empty_every_matrix(self) -> None:
        result = self.matrices([f'build\tmiss\tfrr\tamd64\t{REVISION}\t'], changed='false')
        self.assertEqual(result, {
            'build-matrix': {'include': []}, 'build-extra-matrix': {'include': []},
            'restore-matrix': {'include': []}})

    def test_hits_are_batched_in_eights_without_mixing_runners(self) -> None:
        rows = [f'build\thit\tpackage-{index}\t{arch}\t{REVISION}\t'
                for arch in ('amd64', 'arm64') for index in range(9)]
        self.keys.write_text(''.join(
            f'cache-v2-package-{index}-{arch}-{REVISION}-{NAMESPACE}-122-1\n'
            for arch in ('amd64', 'arm64') for index in range(9)))
        batches = self.matrices(rows)['restore-matrix']['include']
        self.assertEqual([len(batch['entries']) for batch in batches], [8, 1, 8, 1])
        self.assertEqual([batch['runner_label'] for batch in batches],
                         ['ubuntu-24.04', 'ubuntu-24.04', 'ubuntu-24.04-arm', 'ubuntu-24.04-arm'])
        self.assertEqual(len({(entry['package'], entry['arch'])
                              for batch in batches for entry in batch['entries']}), 18)

    def test_workflow_has_one_restore_step_for_each_batch_slot(self) -> None:
        workflow = FILTER.parent.parent / '.github/workflows/publish.yaml'
        slots = re.findall(r'if: \$\{\{ matrix.entries\[(\d+)\] != null \}\}', workflow.read_text())
        self.assertEqual(slots, [str(index) for index in range(8)])


if __name__ == '__main__':
    unittest.main()
