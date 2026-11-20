"""The one door to GitHub: per-identity write budget, retries, audit log and credential handling.

Every request is a `gh api` subprocess. An App installation token reaches it only through the
GH_TOKEN environment variable of that one process; it is never an argument, never logged and never
passed to a request made with the ambient login.
"""
from contextlib import contextmanager
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse

import textsafe

API_VERSION = "2026-03-10"
APP = "app"
USER = "user"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 60


class GhError(Exception):
    """GitHub answered with an error status, or gh failed before any answer (status 0)."""

    def __init__(self, status, message, data=None):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.data = data


class Refused(Exception):
    """A write the tracker declined to send; nothing reached GitHub."""


class RateLimited(Exception):
    """The budget or GitHub asks for a wait longer than the caller allows, or retries ran out."""


class StateError(Exception):
    """The persisted rate-limit state exists but cannot be trusted; sending without it could break a cooldown."""


class Unconfigured(Exception):
    """A credential or setting the command needs is absent."""


def state_dir(env):
    """Return the directory holding the rate-limit state, locks and the default audit log."""
    if env.get("TRACKER_STATE_DIR"):
        return env["TRACKER_STATE_DIR"]
    if env.get("XDG_STATE_HOME"):
        return os.path.join(env["XDG_STATE_HOME"], "tracker")
    return os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state", "tracker")


def _lock(handle, max_wait):
    """Take an exclusive lock, polling until a monotonic deadline; a holder that outlasts max_wait is a rate-limit style wait."""
    deadline = time.monotonic() + max_wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RateLimited(f"another tracker process held a lock longer than the {max_wait:.0f}s allowed") from None
            time.sleep(0.05)


class Limiter:
    """A token bucket per identity, persisted under a lock file so every process shares it.

    A write waits for the minimum gap, the per-minute and the per-hour cap, and any block a retry-after
    or reset header recorded. A wait longer than max_wait raises instead of sleeping.
    """

    def __init__(self, directory, clock=None, sleep=None, min_gap=1.2, per_minute=80, per_hour=450, max_wait=600):
        self.directory = directory
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.min_gap, self.per_minute, self.per_hour, self.max_wait = min_gap, per_minute, per_hour, max_wait

    @contextmanager
    def _locked(self, wait=None):
        os.makedirs(self.directory, exist_ok=True)
        with open(os.path.join(self.directory, "ratelimit.lock"), "a") as lock:
            _lock(lock, self.max_wait if wait is None else wait)
            path = os.path.join(self.directory, "ratelimit.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    state = json.load(handle)
                if not isinstance(state, dict):
                    raise ValueError("not an object")
            except FileNotFoundError:
                state = {}
            except (OSError, ValueError):
                raise StateError(f"the rate-limit state file {path} is unreadable or damaged; fix or remove it (its cooldowns would be lost) before sending") from None
            yield state
            scratch = f"{path}.{os.getpid()}"
            with open(scratch, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(scratch, path)

    def acquire(self, identity, write=True, count=True):
        """Wait until this identity may send, then spend a write if it is one.

        A write with count False is paced by the minimum gap and any block but is not counted against the
        per-minute and per-hour caps (a render is a read in POST clothing).
        """
        deadline = self.clock() + self.max_wait
        while True:
            with self._locked(max(deadline - self.clock(), 0)) as state:
                now = self.clock()
                bucket = state.setdefault(identity, {"last": 0, "stamps": [], "blocked_until": 0})
                stamps = [t for t in bucket["stamps"] if now - t < 3600]
                waits = [bucket["blocked_until"] - now]
                if write:
                    recent = [t for t in stamps if now - t < 60]
                    waits.append(bucket["last"] + self.min_gap - now)
                    if count and len(recent) >= self.per_minute:
                        waits.append(recent[-self.per_minute] + 60 - now)
                    if count and len(stamps) >= self.per_hour:
                        waits.append(stamps[-self.per_hour] + 3600 - now)
                wait = max(waits)
                if wait <= 0:
                    if write:
                        if count:
                            stamps.append(now)
                        bucket["last"] = now
                    bucket["stamps"] = stamps
                    return
                if wait > deadline - now:
                    raise RateLimited(f"the {identity} identity must wait {wait:.0f}s, beyond what is left of the {self.max_wait:.0f}s allowed")
            self.sleep(wait)

    def block(self, identity, until):
        """Record that this identity may not send before the given time."""
        with self._locked() as state:
            bucket = state.setdefault(identity, {"last": 0, "stamps": [], "blocked_until": 0})
            bucket["blocked_until"] = max(bucket["blocked_until"], until)


class Response:
    """A parsed gh api answer."""

    def __init__(self, status, headers, data):
        self.status, self.headers, self.data = status, headers, data


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class Client:
    """Sends gh api requests for the two identities and keeps the audit log."""

    def __init__(self, env, clock=None, sleep=None):
        self.env = dict(env)
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.directory = state_dir(self.env)
        try:
            max_wait = float(self.env.get("TRACKER_MAX_WAIT", 600))
        except ValueError:
            max_wait = -1
        if not math.isfinite(max_wait) or max_wait < 0:
            raise Unconfigured("TRACKER_MAX_WAIT must be a number of seconds, zero or more")
        self.limiter = Limiter(self.directory, self.clock, self.sleep, max_wait=max_wait)
        self.audit_path = self.env.get("TRACKER_AUDIT_LOG") or os.path.join(self.directory, "audit.jsonl")

    def render(self, text, repo, identity=APP):
        """Return the HTML GitHub renders for text as a comment in repo's context; paced, never audited or counted as a write."""
        payload = {"text": text, "mode": "gfm", "context": repo}
        answer = self._send("POST", "/markdown", payload, identity, None, audit=False, count=False)
        html = "" if answer.data is None else answer.data
        if (answer.status != 200 or not answer.headers.get("content-type", "").lower().startswith("text/html")
                or not isinstance(html, str) or (text.strip() and not html.strip())):
            raise GhError(0, "GitHub's markdown answer was not rendered HTML; it is never read as inert")
        return html

    def graphql(self, query, variables):
        """Run a GraphQL read as the ambient login; paced, not audited or counted as a write. Returns the parsed answer."""
        answer = self._send("POST", "graphql", {"query": query, "variables": variables}, USER, None, audit=False, count=False).data
        if not isinstance(answer, dict) or answer.get("errors") or not isinstance(answer.get("data"), dict):
            raise GhError(0, "GitHub's GraphQL answer was missing or reported errors")
        return answer

    def get(self, path, identity=USER):
        """Read a path. Reads are never audited and spend no write budget."""
        return self._send("GET", path, None, identity, None)

    def write(self, method, path, body=None, identity=APP, issue=None, baseline=None):
        """Send a content write after sweeping its final payload; baseline names text whose existing hits may stay."""
        if body is not None:
            try:
                found = textsafe.new_hits("\n".join(_strings(body)), baseline or "")
            except textsafe.SweepUnavailable as unavailable:
                self._audit(identity, method, path, issue, 0, "refused", 0, reason="sweep: patterns unavailable")
                raise Refused(f"write refused: the payload cannot be checked: {unavailable}") from None
            if found:
                kinds = ", ".join(sorted({hit.kind for hit in found}))
                self._audit(identity, method, path, issue, 0, "refused", 0, reason=f"sweep: {kinds}")
                raise Refused(f"write refused: the payload holds {len(found)} sweep hit(s) ({kinds})")
        return self._send(method, path, body, identity, issue)

    @contextmanager
    def issue_lock(self, repo, number):
        """Hold the exclusive per-issue lock for a read-modify-write of one issue."""
        os.makedirs(self.directory, exist_ok=True)
        name = "issue-" + re.sub(r"[^A-Za-z0-9._-]", "-", f"{repo}-{number}") + ".lock"
        with open(os.path.join(self.directory, name), "a") as lock:
            _lock(lock, self.limiter.max_wait)
            yield

    def _env(self, identity):
        env = dict(self.env)
        token = env.pop("TRACKER_APP_TOKEN", None)
        if identity == APP:
            for name in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
                env.pop(name, None)
            env["GH_TOKEN"] = token
        env.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
        return env

    def _redact(self, text):
        for secret in (self.env.get("TRACKER_APP_TOKEN"), self.env.get("GH_TOKEN"), self.env.get("GITHUB_TOKEN")):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _audit(self, identity, method, path, issue, size, status, duration, reason=None):
        entry = {
            "ts": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "identity": identity, "method": method, "endpoint": path.split("?", 1)[0], "issue": issue,
            "bytes": size, "status": status, "duration_ms": round(duration * 1000),
        }
        if reason:
            entry["reason"] = reason
        try:
            os.makedirs(os.path.dirname(self.audit_path) or ".", exist_ok=True)
            descriptor = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, (json.dumps(entry) + "\n").encode())
            finally:
                os.close(descriptor)
        except OSError as error:
            # The write has already happened; losing its audit line must not turn it into a failure.
            sys.stderr.write(f"tracker: could not append to the audit log ({error.strerror})\n")

    def audit_refusal(self, identity, label, reason):
        """Record a write the tool refused before any request existed; the label names the text, never its content."""
        self._audit(identity, "-", label, None, 0, "refused", 0, reason=reason)

    def _send(self, method, path, body, identity, issue, audit=True, count=True):
        if identity == APP and not self.env.get("TRACKER_APP_TOKEN"):
            raise Unconfigured("TRACKER_APP_TOKEN is not set; App requests need the installation token")
        payload = json.dumps(body) if body is not None else None
        size = len(payload.encode()) if payload is not None else 0
        is_write = method != "GET"
        for attempt in range(MAX_ATTEMPTS):
            self.limiter.acquire(identity, write=is_write, count=count)
            started = time.monotonic()
            try:
                response = self._run(method, path, payload, identity)
            except GhError:
                if is_write and audit:
                    self._audit(identity, method, path, issue, size, "error", time.monotonic() - started, reason="no HTTP answer")
                raise
            if is_write and audit:
                self._audit(identity, method, path, issue, size, response.status, time.monotonic() - started)
            wait = self._wait_for(response, attempt)
            if wait is None:
                if response.status >= 400:
                    raise GhError(response.status, self._redact(_message(response.data)), response.data)
                return response
            self.limiter.block(identity, self.clock() + wait)
            if wait > self.limiter.max_wait:
                raise RateLimited(f"GitHub asked the {identity} identity to wait {wait:.0f}s, beyond the {self.limiter.max_wait:.0f}s allowed")
        raise RateLimited(f"still limited after {MAX_ATTEMPTS} attempts")

    def _wait_for(self, response, attempt):
        """Seconds to back off for a limit or spam answer, or None when the answer is final."""
        message = _message(response.data)
        headers = response.headers
        limited = response.status == 429 or (response.status == 403 and (
            "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
            or re.search(r"rate limit|abuse|secondary", message, re.I)))
        spam = response.status == 422 and re.search(r"spam", message, re.I)
        if not (limited or spam):
            return None
        retry = self._seconds(headers.get("retry-after"))
        if retry is not None:
            return retry
        if headers.get("x-ratelimit-remaining") == "0":
            reset = self._seconds(headers.get("x-ratelimit-reset"), epoch=True)
            if reset is not None:
                return max(reset, 1)
        return BACKOFF_SECONDS * 2 ** attempt

    def _seconds(self, value, epoch=False):
        """Seconds to wait from a header: a number, or an HTTP date for Retry-After; None when it is neither."""
        if not value:
            return None
        try:
            number = float(value)
            if math.isfinite(number):
                return number - self.clock() + 1 if epoch else number
        except ValueError:
            pass
        if not epoch:
            try:
                return max(parsedate_to_datetime(value).timestamp() - self.clock(), 1)
            except (TypeError, ValueError, IndexError):
                pass
        return None

    def _run(self, method, path, payload, identity):
        args = ["gh", "api", "-i", "-X", method, "-H", "Accept: application/vnd.github+json",
                "-H", f"X-GitHub-Api-Version: {API_VERSION}", path]
        if payload is not None:
            args += ["--input", "-"]
        try:
            done = subprocess.run(args, input=payload, capture_output=True, text=True, env=self._env(identity), timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GhError(0, self._redact(f"gh could not run: {error}")) from None
        head, separator, text = done.stdout.partition("\n\n") if "\r\n\r\n" not in done.stdout else done.stdout.partition("\r\n\r\n")
        lines = head.splitlines()
        if not separator or not lines or not lines[0].startswith("HTTP/"):
            raise GhError(0, self._redact((done.stderr or done.stdout).strip() or f"gh exited {done.returncode}"))
        status = int(lines[0].split()[1])
        if done.returncode != 0 and status < 400:
            # A success status with a failed gh (the body could not be read) is not an answer to act on.
            raise GhError(0, self._redact((done.stderr or "").strip() or f"gh exited {done.returncode} after a {status} status"))
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = text
        return Response(status, headers, data)


_NEXT = re.compile(r'<([^>]*)>\s*;[^,]*\brel="?next"?')


def next_cursor(response):
    """The `after` cursor a Project item listing's Link header names for its next page, or None on the last page.

    Project items page by cursor, not by page number. A next link with no cursor is an error, never read as the last page.
    """
    found = _NEXT.search(response.headers.get("link") or "")
    if found is None:
        return None
    cursor = urllib.parse.parse_qs(urllib.parse.urlsplit(found.group(1)).query).get("after")
    if not cursor or not cursor[0]:
        raise GhError(0, "GitHub's Link header names a next page with no cursor; not reading that as the last page")
    return cursor[0]


def _message(data):
    if isinstance(data, dict):
        return str(data.get("message", ""))
    return str(data or "")
