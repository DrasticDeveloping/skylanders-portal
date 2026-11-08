"""A stand-in for ctest in skytest's own tests (the `unit` layout). Reads the tests of
`--test-dir <dir>` from <dir>/fake_ctest.txt (one `<name> pass|fail` per line), runs those matching
`-R <regex>`, and answers as ctest does: exit 0 when all passed, 8 when one failed, and with
`--no-tests=error` 8 when none matched (without it, 0)."""
import re
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    test_dir = Path(args[args.index("--test-dir") + 1])
    pattern = re.compile(args[args.index("-R") + 1])
    tests = {}
    listing = test_dir / "fake_ctest.txt"
    if listing.is_file():
        for line in listing.read_text(encoding="utf-8").splitlines():
            if line.strip():
                name, result = line.split()
                tests[name] = result
    ran = {n: r for n, r in tests.items() if pattern.search(n)}
    if not ran:
        print("No tests were found!!!")
        return 8 if "--no-tests=error" in args else 0
    for n, r in ran.items():
        print(f"    Test: {n} ... {'Passed' if r == 'pass' else '***Failed'}")
    if all(r == "pass" for r in ran.values()):
        print("100% tests passed")
        return 0
    print("The following tests FAILED:")
    return 8


if __name__ == "__main__":
    sys.exit(main())
