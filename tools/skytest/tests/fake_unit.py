"""A stand-in for a unit test binary in skytest's own tests (the `unit` layout), with the output of
the portal's skylanders_backend_test. `--mode=` picks what it does: `pass` (the default: its tests,
the summary and the pass line, exit 0), `fail` (a FAIL line, the summary, exit 1), `silent` (exit 0
without the pass line) or `hang` (never ends); `--sleep=<s>` waits that long first (cases that run at
once). It also says where it ran from."""
import sys
import time


def main() -> int:
    mode = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--mode=")), "pass")
    sleep = next((float(a.split("=", 1)[1]) for a in sys.argv[1:] if a.startswith("--sleep=")), 0.0)
    print(f"ran from {sys.argv[0]}")
    time.sleep(sleep)
    if mode == "hang":
        sys.stdout.flush()
        time.sleep(120)
        return 0
    if mode == "silent":
        return 0
    print("ok ID1 IdRoundTrip (1 ms)")
    print("ok ID2 IdHashMatchesSession (0 ms)")
    if mode == "fail":
        print("FAIL tests/backend_entity_id_test.cpp:42: id.Valid()", file=sys.stderr)
        print("FAIL ID3 IdAmbiguousNeverPicked (2 ms)")
        print("3 backend tests, 1 failed, 5 ms")
        return 1
    print("ok ID3 IdAmbiguousNeverPicked (2 ms)")
    print("3 backend tests, 0 failed, 5 ms")
    print("all backend tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
