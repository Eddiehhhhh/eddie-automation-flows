from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_private_proof as proof


class RefSource:
    def __init__(self, refs):
        self.refs = list(refs)
        self.calls = 0

    def metadata(self):
        return {'full_name': proof.REPOSITORY, 'private': True}

    def ref(self, name):
        self.assert_ref(name)
        self.calls += 1
        return self.refs.pop(0) if self.refs else None

    def assert_ref(self, name):
        if name != proof.REF:
            raise AssertionError(name)


class PrivateProofRefRetry(unittest.TestCase):
    def test_missing_ref_once_retries_and_accepts_valid_head(self):
        source = RefSource([None, 'a' * 40])
        self.assertEqual(proof.PrivateRecords(source).guard(), 'a' * 40)
        self.assertEqual(source.calls, 2)

    def test_persistent_missing_ref_still_fails_closed(self):
        source = RefSource([None, None, 'a' * 40])
        with self.assertRaises(proof.PrivateProofError):
            proof.PrivateRecords(source).guard()
        self.assertEqual(source.calls, 2)

    def test_malformed_ref_is_not_retried(self):
        source = RefSource(['not-a-commit', 'a' * 40])
        with self.assertRaises(proof.PrivateProofError):
            proof.PrivateRecords(source).guard()
        self.assertEqual(source.calls, 1)


if __name__ == '__main__':
    unittest.main()
