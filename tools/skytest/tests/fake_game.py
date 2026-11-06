"""A stand-in for a game exe in skytest's own tests: reads the settings the runner passes, plays the
input script's commands fast, and writes the log lines the real game writes.

Extra commands only this fake knows: `hang` (never quits), `crash` (logs a crash and exits 3),
`exit <code>`. `place` logs the hub as the level, as a figure placed after a save is loaded does.
`online_host` writes a hosted server's log into the session folder as the real owner's game does
(its frame-time windows with the p95 of `serverframes <ms>`, 12 by default); `online_join <addr>`
logs the connection when the address has the owner's port. `savewrite` rewrites the first save under
the data folder's <profile>/415608E9 (as a save the game writes); `savewrite backup` writes only into
the data folder's save-backup (as the SDK does), which is no save. `serverlog <text>` appends a line
to the hosted server's log (a [compare] case's workload); with `--render_hidden_draw=false` the
hosted server's log says the draw skip is on, as the real server's does."""
import os
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
        server_p95 = 12.0
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
            elif cmd == "fps0":  # a frozen game: no frame in the window
                line("Input script: 0.0 guest FPS (0 frames in 5.00 s), longest frame 0 ms, 0 over 50 ms")
            elif cmd == "scribble":  # a game that writes where it must not: into its disc folder
                with open(arg("game_data_root") + "/scribble.txt", "w", encoding="utf-8") as f:
                    f.write("x")
            elif cmd == "ownerwrite":  # a game that writes into the owner's data folder
                target = os.path.join(os.environ["USERPROFILE"], "Documents", "Skylanders")
                os.makedirs(target, exist_ok=True)
                with open(os.path.join(target, "oops.txt"), "w", encoding="utf-8") as f:
                    f.write("x")
            elif cmd == "serverframes":
                server_p95 = float(rest)
            elif cmd == "online_host":
                session = os.path.join(arg("user_data_root"), "Online", "session")
                os.makedirs(session, exist_ok=True)
                with open(os.path.join(session, "game.log"), "w", encoding="utf-8") as s:
                    now = time.time()
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + ".000"
                    s.write(f"[{stamp}] [info] [core] [t2] Online: starting as the server\n")
                    s.write(f"[{stamp}] [info] [core] [t2] Online: frames 0 in 10.0 s (no frame presented)\n")
                    s.write(f"[{stamp}] [info] [core] [t2] Level watch: tutorial_b: ready (request 1, a player is in it)\n")
                    s.write(f"[{stamp}] [info] [core] [t2] Online: frames 600 fps 60.0 p50 16.7 p95 {server_p95} "
                            "max 40.0 private 1500 working 900 video 410\n")
                    if arg("render_hidden_draw") == "false":
                        s.write(f"[{stamp}] [info] [gpu] [t3] GPU: draw skip on (render_hidden_draw off, window "
                                "hidden): draws without memexport, resolves and presents are skipped\n")
                line(f"Online: hosting: the server is on port {arg('online_port')}, join code ABCDEFGH")
            elif cmd == "serverlog":
                now = time.time()
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
                with open(os.path.join(arg("user_data_root"), "Online", "session", "game.log"), "a",
                          encoding="utf-8") as s:
                    s.write(f"[{stamp}] [info] [core] [t2] {rest}\n")
            elif cmd == "online_join":
                if rest.endswith(":" + rest.rsplit(":", 1)[-1]) and rest.rsplit(":", 1)[-1].isdigit():
                    line(f"Online: online_join: connected to {rest}")
                else:
                    line(f"Online: online_join {rest}: no server", "error")
            elif cmd == "savewrite":
                data = arg("user_data_root")
                if rest == "backup":
                    os.makedirs(os.path.join(data, "save-backup", "previous"), exist_ok=True)
                    with open(os.path.join(data, "save-backup", "previous", "package"), "wb") as f:
                        f.write(b"old")
                    line("Content: SWAPFORCE_0 saved; the version before it is in save-backup")
                else:
                    target = next(os.path.join(d, f) for d, _, files in sorted(os.walk(data))
                                  if os.sep + "415608E9" in d for f in sorted(files))
                    with open(target, "ab") as f:
                        f.write(b"progress")
                    line(f"Content: saving {os.path.relpath(target, data)} anew")
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
