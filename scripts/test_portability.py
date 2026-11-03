"""Prevent deployment identities from becoming repository defaults again."""
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parent.parent
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__'}
IDENTITIES = re.compile(r'\b(?:grace|love)\b|fourth-gantry|andrew-61005|HC_Volume', re.I)
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')


class PortabilityTests(unittest.TestCase):
    def test_no_machine_specific_defaults(self):
        failures = []
        for path in SOURCE.rglob('*'):
            if (not path.is_file() or EXCLUDED.intersection(path.relative_to(SOURCE).parts)
                    or path.name == 'uv.lock' or path == Path(__file__).resolve()):
                continue
            try:
                content = path.read_text()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if IDENTITIES.search(line) or any(
                    address != '127.0.0.1' for address in IPV4.findall(line)
                ):
                    failures.append(f'{path.relative_to(SOURCE)}:{number}')
        self.assertEqual(failures, [], 'Machine-specific values in: ' + ', '.join(failures))


if __name__ == '__main__':
    unittest.main()
