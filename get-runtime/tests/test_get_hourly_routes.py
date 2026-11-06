import unittest
from pathlib import Path


class HourlyRoutes(unittest.TestCase):
    def test_hourly_get_skips_legacy_notion_and_manual_routes_remain(self):
        template = (Path(__file__).resolve().parents[1] / '.github/workflow-templates/get-canonical-controller.yml.disabled').read_text()
        self.assertIn("    - cron: '17 * * * *'", template)
        for name in ('Validate bounded Notion route before Get', 'Validate complete private Notion route before Get', 'Sync confirmed Get content to Notion'):
            section = template.split('      - name: ' + name, 1)[1].split('      - name:', 1)[0]
            self.assertIn("github.event_name == 'workflow_dispatch'", section)
            self.assertIn("inputs.notion_mode != 'canonical'", section)
        self.assertIn("NOTION_MODE\" = historical-capture", template)
        self.assertIn("NOTION_MODE\" = historical-apply", template)
        self.assertNotIn('continue-on-error:', template)


if __name__ == '__main__':
    unittest.main()
