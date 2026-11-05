"""A stand-in for a game exe in skytest's own tests: reads the settings the runner passes, plays the
input script's commands fast, and writes the log lines the real game writes.

Extra commands only this fake knows: `hang` (never quits), `crash` (logs a crash and exits 3),
`exit <code>`. `place` logs the hub as the level, as a figure placed after a save is loaded does."""
import sys
import time


def arg(name: str) -> str:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return ""


def main() -> int:
    log_path, script_path = arg("log_file"), arg("input_script")
    with open(log_path, "w", encoding="utf-8") as log:
        def line(text: str, level: str = "info") -> None:
            now = time.time()
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
            log.write(f"[{stamp}] [{level}] [core] [t1] {text}\n")
            log.flush()

        line("Skylanders: Swap Force test (build fake123); Windows")
        line(f"Input script: playing '{script_path}'")
        for raw in open(script_path, encoding="utf-8").read().splitlines():
            cmd, _, rest = raw.strip().partition(" ")
            if not cmd or cmd.startswith("#"):
                continue
            if cmd == "wait":
                time.sleep(float(rest) / 100)  # a hundred times faster than the game
            elif cmd == "log":
                line(f"Input script: {rest}")
            elif cmd == "place":
                line("Input script: placed in slot 0")
                time.sleep(0.05)
                line("Where: town_state10 at 325.04 -2819.78 203.76")
            elif cmd == "fps":
                line("Input script: 44.7 guest FPS (358 frames in 8.00 s), longest frame 27 ms, 0 over 50 ms")
                line("Input script: memory 3113 MB private, 918 MB working set, 1456 handles")
            elif cmd == "hang":
                time.sleep(3600)
            elif cmd == "crash":
                line("Crash: exception C0000005 on thread 1; call chain: sub_82000000", "error")
                return 3
            elif cmd == "exit":
                return int(rest)
            elif cmd == "quit":
                line("Input script: quit")
                return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
