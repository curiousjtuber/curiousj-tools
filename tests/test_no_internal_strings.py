"""Shipped files carry only generic examples: no work, host or account names."""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLOCKLIST = re.compile(
    r"(?i)amazon|rabbit|lapin|brazil|peru|zatanna|flex|midway|\bcld\b|weblab|transporter"
    r"|workplace|kiro|hwjlee|hjlee|/home/curiousj|curiousj@|laptop|dot-files|cloud desktop"
    r"|mac-mini|cachyos-|ryzen"
)


class NoInternalStrings(unittest.TestCase):
    def test_shipped_files(self):
        files = [ROOT / "README.md", ROOT / "pyproject.toml"]
        files += sorted((ROOT / "src").rglob("*.py"))
        files += sorted(p for p in (ROOT / "examples").iterdir() if p.is_file())
        hits = []
        for path in files:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if BLOCKLIST.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
