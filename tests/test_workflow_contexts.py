import copy
from pathlib import Path
import sys
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import get_workflow_contexts as check

class Contexts(unittest.TestCase):
    def test_current_controller_contexts_and_private_consumers(self):
        workflow = yaml.load((ROOT / '.github/workflow-templates/get-canonical-controller.yml.disabled').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(check.validate(workflow), [])
        job = workflow['jobs']['canonical-get']
        self.assertNotIn('GET_PRIVATE_BINDINGS_FILE', job.get('env', {}))
        consumers = {'Materialize verified private runtime data', 'Validate complete private Notion route before Get', 'Emit verified canonical publication anchor', 'Sync confirmed Get content to Notion'}
        seen = set()
        for step in job['steps']:
            if step.get('name') in consumers:
                self.assertEqual(step['env']['GET_PRIVATE_BINDINGS_FILE'], '${{ runner.temp }}/get-private-bindings/runtime-bindings.json')
                seen.add(step['name'])
        self.assertEqual(seen, consumers)
        self.assertNotIn('GITHUB_ENV', str(workflow))
    def test_original_regression_job_runner_rejected(self):
        workflow = {'jobs': {'job': {'env': {'PATH': '${{ runner.temp }}/data'}}}}
        self.assertEqual(check.validate(workflow)[0]['unavailable_contexts'], ['runner'])
    def test_workflow_runner_rejected(self):
        self.assertTrue(check.validate({'env': {'PATH': '${{ runner.temp }}'}}))
    def test_step_runner_accepted(self):
        self.assertFalse(check.validate({'jobs': {'job': {'steps': [{'env': {'PATH': '${{ runner.temp }}'}}]}}}))
    def test_job_if_runner_rejected(self):
        self.assertTrue(check.validate({'jobs': {'job': {'if': "${{ runner.os == 'Linux' }}"}}}))
    def test_bracket_context_rejected(self):
        self.assertTrue(check.validate({'env': {'PATH': "${{ runner['temp'] }}"}}))
    def test_literal_context_word_not_expression(self):
        self.assertFalse(check.validate({'env': {'PATH': 'runner.temp'}}))
    def test_quoted_words_not_context(self):
        self.assertFalse(check.validate({'env': {'PATH': "${{ inputs.value || 'runner.temp' }}"}}))

if __name__ == '__main__': unittest.main()
