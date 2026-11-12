"""Pair one of our rooms with a competitor's room on another website (Agoda, Goibibo).

The same thing as choosing it under "<website> settings: website, gap and
rooms" on the Repricing page, done through the API: the rule for that website
is read, ONE room's pair is set, and everything else on it -- the gap, the
board, the other rooms' pairs -- is sent back exactly as it was.

Shows the change and stops unless ``--apply`` is given. Asks for the password
itself (never pass it on the command line). Standard library only, so it runs
with any Python 3.10+.

    python scripts/set_channel_pair.py --server https://hpm.103.178.166.6.sslip.io \
        --user <login> --channel Agoda --room "Junior Suite" \
        --hotel Ananthyam --theirs "Villa with Garden View"            # look
    ... --apply                                                          # save

``--ours`` is our room's name on that website when it differs from its own
name (on Goibibo, Junior Suite is "Luxury villa").
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(server: str, method: str, path: str, token: str | None = None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"{method} {path} failed: HTTP {exc.code} {detail}")


def norm(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--channel", required=True, help="RMS channel name, e.g. Agoda or Goibibo")
    ap.add_argument("--room", required=True, help="our room's own name, e.g. 'Junior Suite'")
    ap.add_argument("--hotel", required=True, help="the competitor to follow (part of its name)")
    ap.add_argument("--theirs", required=True, help="their room's name on that website")
    ap.add_argument("--ours", help="our room's name on that website, when different")
    ap.add_argument("--apply", action="store_true", help="save; without it nothing changes")
    args = ap.parse_args()

    import os
    password = os.environ.get("HPM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    token = call(args.server, "POST", "/api/v1/auth/login",
                 body={"username": args.user, "password": password})["access_token"]

    rules = call(args.server, "GET", "/api/v1/repricing/channels", token)
    rule = next((r for r in rules if norm(r["channel"]) == norm(args.channel)), None)
    if rule is None:
        sys.exit(f"No rule for {args.channel!r}. Rules there: {[r['channel'] for r in rules]}")

    hotels = call(args.server, "GET", "/api/v1/hotels?limit=200", token)["items"]
    matches = [h for h in hotels if norm(args.hotel) in norm(h["name"])]
    if len(matches) != 1:
        sys.exit(f"--hotel {args.hotel!r} matches {[h['name'] for h in matches] or 'nothing'}; be more exact")
    competitor = matches[0]

    # Our room: one of the rooms the main rule maps (those are the ids pairs use).
    settings = call(args.server, "GET", "/api/v1/repricing/settings", token)
    ours_ids = {int(k) for k in (settings.get("benchmark_room_pairs") or {})} | {int(k) for k in rule["room_pairs"]}
    room = None
    for h in hotels:
        if h["id"] == competitor["id"]:
            continue
        for r in call(args.server, "GET", f"/api/v1/hotels/{h['id']}/rooms", token):
            if r["id"] in ours_ids and norm(r["name"]) == norm(args.room):
                room = r
        if room:
            break
    if room is None:
        sys.exit(f"None of your mapped rooms is called {args.room!r}")

    pairs = {str(k): v for k, v in rule["room_pairs"].items()}
    before = pairs.get(str(room["id"]))
    after = {"ours": args.ours or (before or {}).get("ours"), "theirs": args.theirs,
             "hotel_id": competitor["id"], "board": (before or {}).get("board"), "copies_main": False}
    pairs[str(room["id"])] = after

    print(f"{rule['channel']}: {room['name']} (room {room['id']})")
    print(f"  before: {json.dumps(before)}")
    print(f"  after:  {json.dumps(after)}  -> {competitor['name']}")
    print(f"  other rooms on {rule['channel']} stay as they are: {len(pairs) - 1}")
    if not args.apply:
        print("Nothing saved. Run again with --apply to save.")
        return

    body = {k: rule[k] for k in ("source_id", "benchmark_undercut", "benchmark_with_tax", "benchmark_meal_plan")}
    body["room_pairs"] = pairs
    saved = call(args.server, "PUT", f"/api/v1/repricing/channels/{urllib.parse.quote(rule['channel'])}",
                 token, body)
    print(f"Saved. {room['name']} on {saved['channel']} now: {json.dumps(saved['room_pairs'].get(str(room['id'])))}")


if __name__ == "__main__":
    main()
