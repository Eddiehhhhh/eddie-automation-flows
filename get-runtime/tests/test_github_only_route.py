import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('route', ROOT / 'scripts/get_notion_route_selection.py')
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

class GithubOnly(unittest.TestCase):
    def test_only_explicit_manual_github_mode(self):
        for apply in (True, False):
            self.assertTrue(route.preflight('workflow_dispatch', apply, 'github-only', ''))
        for event, digest in [('schedule', ''), ('workflow_dispatch', 'bad')]:
            with self.assertRaises(route.SelectionError):
                route.preflight(event, True, 'github-only', digest)
        self.assertTrue(route.preflight('schedule', True, 'canonical', ''))
        self.assertTrue(route.preflight('workflow_dispatch', True, 'canonical', ''))

    def test_get_precedes_notion_and_github_mode_skips_notion(self):
        path=ROOT/'.github/workflow-templates/get-canonical-controller.yml.disabled'
        text=path.read_text()
        get=text.index('name: Emit verified canonical publication anchor')
        pre=text.index('name: Validate canonical Notion route after Get')
        sync=text.index('name: Sync confirmed Get content to Notion')
        self.assertLess(get,pre)
        self.assertLess(pre,sync)
        self.assertIn("inputs.notion_mode != 'github-only'",text[sync:])
        self.assertIn("steps.notion_route.outcome == 'success'",text[sync:])
        self.assertIn('continue-on-error: true',text[pre:sync])
        self.assertIn('python scripts/get_recurring_runtime.py',text[get:pre])
        self.assertIn('--apply --scope-from-policy',text[get:pre])

    def test_exact_template_hashes(self):
        folder=ROOT/'.github/workflow-templates'
        old=(folder/'get-canonical-controller-pre-github-only.yml.disabled').read_bytes()
        new=(folder/'get-canonical-controller.yml.disabled').read_bytes()
        self.assertEqual(hashlib.sha256(old).hexdigest(),'31623366601a23a56033eae8bb3b09bf314919b6c00b4905797eec26b6675df3')
        self.assertEqual(hashlib.sha256(new).hexdigest(),'f4956711718296718e25ee126ab6fdb72623be1343e8a1c0f0336c50b523e87e')

if __name__=='__main__': unittest.main()
