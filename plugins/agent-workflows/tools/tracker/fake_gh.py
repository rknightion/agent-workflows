#!/usr/bin/env python3
"""A stand-in for the gh CLI used by the tracker tests.

It answers 'gh api -X METHOD PATH [--input -]' from a route table and records every call. FAKE_GH_DIR holds
routes.json (a list of {pattern, responses}) and calls.jsonl. A pattern is a regular expression matched in full
against "METHOD /path?query"; each match consumes the next response, the last one repeating. No route answers 404,
so a missing route fails a test loudly. An error answer goes to stderr as gh writes it ("gh: <message> (HTTP <n>)")
with exit 1.
"""
import json
import os
import re
import sys


def main(argv):
    root = os.environ["FAKE_GH_DIR"]
    if argv[:1] != ["api"]:
        print("fake gh only implements gh api", file=sys.stderr)
        return 1
    args = argv[1:]
    method, path, use_input = "GET", None, False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-X":
            method, index = args[index + 1], index + 2
        elif arg == "--input":
            use_input, index = True, index + 2
        else:
            path, index = arg, index + 1
    stdin = sys.stdin.read() if use_input else None
    record = {"method": method, "path": path, "body": json.loads(stdin) if stdin else None, "argv": argv,
              "gh_token": os.environ.get("GH_TOKEN"), "tracker_app_token": os.environ.get("TRACKER_APP_TOKEN")}
    with open(os.path.join(root, "calls.jsonl"), "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    routes_path = os.path.join(root, "routes.json")
    with open(routes_path, encoding="utf-8") as handle:
        routes = json.load(handle)
    key = f"{method} {path}"
    response = {"status": 404, "body": {"message": "no fake route for " + key}}
    for route in routes:
        if re.fullmatch(route["pattern"], key):
            used = route.get("used", 0)
            response = route["responses"][min(used, len(route["responses"]) - 1)]
            route["used"] = used + 1
            break
    with open(routes_path, "w", encoding="utf-8") as handle:
        json.dump(routes, handle)
    status, body = response.get("status", 200), response.get("body")
    if status >= 400:
        message = body.get("message", "error") if isinstance(body, dict) else str(body)
        print(f"gh: {message} (HTTP {status})", file=sys.stderr)
        return 1
    if body is not None:
        sys.stdout.write(body if isinstance(body, str) else json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
