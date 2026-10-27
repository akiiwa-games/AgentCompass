import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

import unpack_files


def block(name, content, claimed=None):
    sha = claimed or hashlib.sha256(content.encode('utf-8')).hexdigest()
    return f'=== FILE: {name} sha256={sha} ===\n{content}=== END FILE ===\n'


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = Path(self.directory.name) / 'out'

    def test_files_are_written_and_verified_including_nested_paths_and_unicode(self):
        reply = 'EXECUTED: yes\n' + block('a.py', 'x = 1\n') + block('pkg/b_test.py', 'print("é—日本")\n') + 'NOTES: none\n'
        results = unpack_files.unpack(reply, self.out)
        self.assertEqual(results, [('a.py', 'ok'), ('pkg/b_test.py', 'ok')])
        self.assertEqual((self.out / 'a.py').read_bytes(), b'x = 1\n')
        self.assertEqual((self.out / 'pkg' / 'b_test.py').read_bytes(), 'print("é—日本")\n'.encode('utf-8'))

    def test_the_runners_crlf_is_normalized_before_hashing(self):
        reply = (block('a.py', 'x = 1\ny = 2\n')).replace('\n', '\r\n')
        self.assertEqual(unpack_files.unpack(reply, self.out), [('a.py', 'ok')])
        self.assertEqual((self.out / 'a.py').read_bytes(), b'x = 1\ny = 2\n')

    def test_a_file_without_a_final_newline_is_accepted_when_that_is_what_was_hashed(self):
        sha = hashlib.sha256(b'no newline').hexdigest()
        reply = f'=== FILE: a.txt sha256={sha} ===\nno newline\n=== END FILE ===\n'
        self.assertEqual(unpack_files.unpack(reply, self.out), [('a.txt', 'ok')])
        self.assertEqual((self.out / 'a.txt').read_bytes(), b'no newline')

    def test_a_wrong_hash_writes_nothing_unless_asked_to_keep_it_unverified(self):
        reply = block('a.py', 'x = 1\n', claimed='0' * 64)
        self.assertEqual(unpack_files.unpack(reply, self.out), [('a.py', 'mismatch')])
        self.assertEqual(list(self.out.iterdir()), [])
        other = Path(self.directory.name) / 'other'
        self.assertEqual(unpack_files.unpack(reply, other, keep_unverified=True), [('a.py', 'mismatch')])
        self.assertTrue((other / 'a.py.unverified').exists())
        self.assertFalse((other / 'a.py').exists())

    def test_unsafe_names_are_refused(self):
        for name in ('../evil.py', '/etc/passwd', 'a/../../b.py', '.hidden', 'a//b.py', 'C:\\x.py', 'a\\b.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                reply = block(name, 'x\n')
                with self.assertRaises(ValueError):
                    unpack_files.unpack(reply, Path(directory) / 'o')

    def test_a_non_empty_output_directory_is_refused_and_a_missing_end_line_is_an_error(self):
        self.out.mkdir()
        (self.out / 'keep.txt').write_text('mine')
        with self.assertRaises(ValueError):
            unpack_files.unpack(block('a.py', 'x\n'), self.out)
        with self.assertRaises(ValueError):
            unpack_files.parse('=== FILE: a.py sha256=' + '0' * 64 + ' ===\nx\n')

    def test_the_exit_code_is_zero_only_when_every_file_verified(self):
        base = Path(self.directory.name)
        reply = base / 'reply.md'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            reply.write_text(block('a.py', 'x\n'), encoding='utf-8', newline='\n')
            self.assertEqual(unpack_files.main([str(reply), str(base / 'o1')]), 0)
            reply.write_text(block('a.py', 'x\n', claimed='1' * 64), encoding='utf-8', newline='\n')
            self.assertEqual(unpack_files.main([str(reply), str(base / 'o2')]), 1)
            reply.write_text('no blocks here\n', encoding='utf-8', newline='\n')
            self.assertEqual(unpack_files.main([str(reply), str(base / 'o3')]), 1)
            self.assertEqual(unpack_files.main([str(base / 'missing.md'), str(base / 'o4')]), 2)


if __name__ == '__main__':
    unittest.main()
