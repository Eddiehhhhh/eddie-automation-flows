import unittest
from pathlib import Path


class HourlyRoutes(unittest.TestCase):
    def test_hourly_get_skips_legacy_notion_and_manual_routes_remain(self):
        template = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        self.assertIn("    - cron: '23 * * * *'", template)
        self.assertIn('permissions:\n  actions: read\n  contents: read\n', template)
        for name in ('Validate bounded Notion route before Get', 'Validate complete private Notion route before Get', 'Sync confirmed Get content to Notion'):
            section = template.split('      - name: ' + name, 1)[1].split('      - name:', 1)[0]
            self.assertIn("github.event_name == 'workflow_dispatch'", section)
            self.assertIn("inputs.notion_mode != 'canonical'", section)
        self.assertIn("NOTION_MODE\" = historical-capture", template)
        self.assertIn("NOTION_MODE\" = historical-apply", template)
        self.assertNotIn('continue-on-error:', template.split('  notion-incremental:', 1)[0])


if __name__ == '__main__':
    unittest.main()

class FollowupNotion(unittest.TestCase):
    def test_incremental_job_follows_successful_get_and_preserves_failure_evidence(self):
        template = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        job = template.split('  notion-incremental:\n', 1)[1]
        self.assertIn('needs: canonical-get', job)
        self.assertIn("needs.canonical-get.result == 'success'", job)
        self.assertIn("inputs.notion_mode == 'canonical'", job)
        self.assertIn("inputs.private_proof_key == ''", job)
        self.assertNotIn("inputs.notion_mode == 'private-single-note-proof'", job)
        self.assertIn('!inputs.create_test_note', job)
        self.assertIn('continue-on-error: true', job)
        self.assertIn('get_notion_incremental_controller as controller', job)
        self.assertIn('RESULT: ${{ job.status }}', job)
        self.assertIn('NOTION_STATE: ${{ steps.notion_queue.outputs.state }}', job)
        self.assertIn('Notion: partial', job)
        self.assertIn('Notion: failed or incomplete', job)
        self.assertIn('echo "$message" >> "$GITHUB_STEP_SUMMARY"', job)
        self.assertNotIn('get_notion_runtime.py', job)
        self.assertIn("ref: '@@RUNTIME_SHA@@'", job)

    def test_private_proof_selector_never_accepts_or_exposes_a_note_id(self):
        template = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        self.assertIn('private_proof_key:', template)
        self.assertIn('inputs.private_proof_key', template)
        self.assertNotIn('single_note_id:', template)
        self.assertNotIn('inputs.single_note_id', template)
