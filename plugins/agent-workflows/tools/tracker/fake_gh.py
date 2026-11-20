#!/usr/bin/env python3
"""A stand-in for the `gh` CLI used by the tracker tests.

It answers `gh api -i -X METHOD PATH [--input -]` from a route table and records every call.
FAKE_GH_DIR holds `routes.json` (a list of {pattern, responses}) and `calls.jsonl`. A pattern is a
regular expression matched in full against "METHOD /path?query"; each match consumes the next
response, the last one repeating; a route marked fallback answers only when no other matches. No route
answers 404, so a missing route fails a test loudly.

A route with an `items` list instead of `responses` is a Project item listing: it serves the list page by
page as GitHub does (`per_page`, default 30; an opaque `after` cursor taken from the `Link` header's
rel="next"), and applies the `q` filter terms `repo:OWNER/NAME`, `status:"NAME"` and `label:"NAME"`
(an item with no `Status` field value never matches a status term).
"""
import json
import os
import re
import sys
import urllib.parse


def _field(item, name):
    for field in item.get("fields") or []:
        if field.get("name") == name:
            value = field.get("value")
            value = value.get("name") if isinstance(value, dict) else value
            return value.get("raw") if isinstance(value, dict) else value
    return None


def _wanted(item, term):
    """Whether one Project item satisfies one `q` term; a term this fake does not know matches nothing, loudly."""
    kind, _, value = term.partition(":")
    value = value.strip('"')
    content = item.get("content") or {}
    if kind == "repo":
        found = re.search(r"github\.com/([^/]+/[^/]+)/", content.get("html_url", ""))
        return bool(found) and found.group(1).lower() == value.lower()
    if kind == "status":
        return (_field(item, "Status") or "").lower() == value.lower()
    if kind == "label":
        return value.lower() in [str(l.get("name", "")).lower() for l in content.get("labels") or []]
    if kind == "is":
        return {"issue": item.get("content_type") == "Issue", "open": content.get("state", "open") == "open"}.get(value, False)
    return False


def paged_items(items, path):
    """The answer of a Project item listing: the filtered items, one page of them, and a Link header when more remain."""
    split = urllib.parse.urlsplit(path)
    query = urllib.parse.parse_qs(split.query)
    terms = re.findall(r'(?:[^\s"]|"[^"]*")+', (query.get("q") or [""])[0])
    matching = [item for item in items if all(_wanted(item, term) for term in terms)]
    size = int((query.get("per_page") or ["30"])[0])
    start = int((query.get("after") or ["c0"])[0][1:])
    # As GitHub does, only the fields asked for with `fields[]=<id>` come back (none, when none is asked for).
    asked = {str(i) for i in query.get("fields[]", [])}
    page = [dict(item, fields=[f for f in item.get("fields") or [] if str(f.get("id")) in asked]) for item in matching[start:start + size]]
    headers = {}
    if start + size < len(matching):
        pairs = [(k, v) for k, vs in query.items() if k != "after" for v in vs] + [("after", f"c{start + size}")]
        headers["Link"] = f'<https://api.github.com{split.path}?{urllib.parse.urlencode(pairs)}>; rel="next"'
    return {"status": 200, "headers": headers, "body": page}


def main(argv):
    root = os.environ["FAKE_GH_DIR"]
    if argv[:1] != ["api"]:
        print("fake gh only implements `gh api`", file=sys.stderr)
        return 1
    args = argv[1:]
    method, path, headers, use_input = "GET", None, [], False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-X":
            method, index = args[index + 1], index + 2
        elif arg == "-H":
            headers.append(args[index + 1])
            index += 2
        elif arg == "--input":
            use_input, index = True, index + 2
        elif arg == "-i":
            index += 1
        else:
            path, index = arg, index + 1
    stdin = sys.stdin.read() if use_input else None
    record = {
        "method": method,
        "path": path,
        "headers": headers,
        "stdin": stdin,
        "argv": argv,
        "gh_token": os.environ.get("GH_TOKEN"),
        "tracker_app_token": os.environ.get("TRACKER_APP_TOKEN"),
    }
    with open(os.path.join(root, "calls.jsonl"), "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    routes_path = os.path.join(root, "routes.json")
    with open(routes_path, encoding="utf-8") as handle:
        routes = json.load(handle)
    key = f"{method} {path}"
    response = {"status": 404, "body": {"message": "no fake route for " + key}}
    if key == "POST /markdown":
        response = {"status": 200, "body": "<p>rendered, nothing live</p>"}  # the default; a test's own route wins
    # A route marked fallback answers only when no test route matches.
    for route in sorted(routes, key=lambda r: bool(r.get("fallback"))):
        if re.fullmatch(route["pattern"], key):
            used = route.get("used", 0)
            responses = route.get("responses")
            response = paged_items(route["items"], path) if responses is None else responses[min(used, len(responses) - 1)]
            route["used"] = used + 1
            break
    with open(routes_path, "w", encoding="utf-8") as handle:
        json.dump(routes, handle)
    if "exit" in response:
        print(response.get("stderr", ""), file=sys.stderr)
        return response["exit"]
    status = response.get("status", 200)
    if key == "POST /markdown" and isinstance(response.get("body"), str):
        headers = response.setdefault("headers", {})
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "text/html; charset=utf-8"  # what the real endpoint sends; a test overrides it to break it
    sys.stdout.write(f"HTTP/2.0 {status} Fixture\r\n")
    for name, value in response.get("headers", {}).items():
        sys.stdout.write(f"{name}: {value}\r\n")
    sys.stdout.write("\r\n")
    body = response.get("body")
    if body is not None:
        sys.stdout.write(body if isinstance(body, str) else json.dumps(body))
    sys.stdout.flush()
    return response.get("exit_after", 1 if status >= 400 else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
