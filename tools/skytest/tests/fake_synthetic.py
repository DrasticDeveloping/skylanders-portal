"""A stand-in for a synthetic peer (stage 4's tools/headless_peer of the Swap Force fork) in skytest's
own tests: takes --port, --script and --digest as the runner passes them, prints them, says it
connected, then plays its script's lines fast:

  swap <n> <hero> <tick>   the swap event pair's reference line
  say <text>               prints the text
  hang                     never ends
  exit <code>              ends with that code
  noconnect                (anywhere) never says it connected
"""
import sys
import time


def main() -> int:
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    text = open(args["--script"], encoding="utf-8").read()
    print(f"Online: synthetic peer args port {args.get('--port')} digest {args.get('--digest')}", flush=True)
    if "noconnect" not in text:
        print("Online: synthetic peer 3 connected (backend mode 4)", flush=True)
    for raw in text.splitlines():
        cmd, _, rest = raw.strip().partition(" ")
        if cmd == "swap":
            n, hero, tick = rest.split()
            print(f"Online: swap {n} hero {hero} epoch 1 gen 2 tick {tick}", flush=True)
        elif cmd == "say":
            print(rest, flush=True)
        elif cmd == "hang":
            time.sleep(3600)
        elif cmd == "exit":
            return int(rest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
