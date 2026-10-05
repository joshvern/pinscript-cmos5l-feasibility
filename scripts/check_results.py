"""Reject missing, empty, all-skipped or failing JUnit/cocotb results.

Optional: --expect N requires exactly N passed test cases across all files, and
--forbid-skips rejects any skipped case, so a dropped or skipped test cannot pass.
"""
import argparse
import sys
import xml.etree.ElementTree as ET


def validate(path: str) -> tuple[int, int]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    # Also honor suite-level counts, including a failure before any case ran.
    suite_failures = sum(int(node.get("failures", "0")) + int(node.get("errors", "0"))
                         for node in root.iter("testsuite"))
    passed = len(cases) - failed - skipped
    if failed or suite_failures or passed < 1:
        raise ValueError(f"{path}: {passed} passed, {failed} failed, {skipped} skipped; nonempty pass required")
    print(f"PASS {path}: {passed} passed, {skipped} skipped")
    return passed, skipped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="+")
    parser.add_argument("--expect", type=int, help="exact number of passed test cases across all files")
    parser.add_argument("--forbid-skips", action="store_true")
    args = parser.parse_args()
    try:
        totals = [validate(filename) for filename in args.results]
        passed, skipped = sum(p for p, _ in totals), sum(s for _, s in totals)
        if args.forbid_skips and skipped:
            raise ValueError(f"{skipped} skipped test case(s) with --forbid-skips")
        if args.expect is not None and passed != args.expect:
            raise ValueError(f"expected exactly {args.expect} passed test cases, found {passed}")
        if args.expect is not None:
            print(f"PASS expected count: {passed} passed")
    except (OSError, ET.ParseError, ValueError) as error:
        sys.exit(str(error))
