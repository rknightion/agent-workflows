"""Mint a GitHub App installation token scoped to one repository (README.md, with-token).

Stdlib only, and independent of tracker.py, so other tools can import it. The App's credentials
lie in the credentials tree, ${CREDENTIALS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/tracker/credentials}/github/<TRACKER_APP_CRED>/:
`app.env` (GITHUB_APP_ID, and GITHUB_APP_SLUG when the App has one) and `private-key.pem`. A JWT is signed with
`openssl` reading the key file where it lies, the App's installation on the repository is found
(GET /repos/<owner>/<name>/installation) and a token is minted for that one repository only
(POST /app/installations/<id>/access_tokens with {"repositories": ["<name>"]}). The token's permissions are the
installation's own; they are not narrowed here. Nothing here prints, logs or exports the JWT, the token or the key.
"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
API_VERSION = "2026-03-10"
CRED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
REPO_SHAPE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SETTING, GITHUB = 2, 4


class MintError(Exception):
    """Minting cannot go on; code is the exit status (2 setting, 4 GitHub). The message never carries the JWT, the token or the key."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id, key_path, now=None):
    """Sign a ten-minute-at-most RS256 JWT for the App; openssl reads the key file, which this never opens."""
    now = int(now if now is not None else time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    try:
        signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_path)], input=f"{head}.{body}".encode(),
                                capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise MintError(SETTING, "openssl could not run") from None
    if signed.returncode != 0 or not signed.stdout:
        raise MintError(SETTING, "openssl could not sign with the App's private key file")
    return f"{head}.{body}.{_b64(signed.stdout)}"


def _call(method, path, jwt, body=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "Authorization": "Bearer " + jwt}
    data = None
    if method == "POST":
        data = json.dumps(body if body is not None else {}).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        message = ""
        try:
            parsed = json.loads(error.read().decode())
            message = parsed.get("message", "") if isinstance(parsed, dict) else ""
        except ValueError:
            pass
        finally:
            error.close()
        raise MintError(GITHUB, f"GitHub answered {error.code} to {method} {path.split('?', 1)[0]}: {str(message)[:200]}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise MintError(GITHUB, f"could not reach GitHub for {path} ({type(error).__name__})") from None
    if not isinstance(answer, dict):
        raise MintError(GITHUB, f"GitHub's answer to {path} is not an object")
    return answer


def read_env_file(path):
    """KEY=VALUE lines (an optional `export `, optional matching quotes); comments and blank lines skipped."""
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise MintError(SETTING, f"cannot read {path}") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].lstrip() if line.startswith("export ") else line
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def credential_dir(env):
    """The App's directory in the credentials tree, named by TRACKER_APP_CRED (a plain name, never a path)."""
    name = env.get("TRACKER_APP_CRED") or ""
    if not CRED_NAME.fullmatch(name) or name in (".", ".."):
        raise MintError(SETTING, "TRACKER_APP_CRED is not set to a directory name under github/ in the credentials tree")
    config = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    root = env.get("CREDENTIALS_DIR") or os.path.join(config, "tracker", "credentials")
    return Path(root) / "github" / name


def mint(env, repo, now=None):
    """Return (installation token scoped to repo alone, `<slug>[bot]` login or None) for the App installed on repo."""
    if not isinstance(repo, str) or not REPO_SHAPE.fullmatch(repo):
        raise MintError(SETTING, "the repository must be owner/name")
    directory = credential_dir(env)
    settings = read_env_file(directory / "app.env")
    app_id = settings.get("GITHUB_APP_ID", "")
    if not re.fullmatch(r"[0-9]+", app_id):
        raise MintError(SETTING, f"{directory / 'app.env'} has no numeric GITHUB_APP_ID")
    key = directory / "private-key.pem"
    if not key.is_file():
        raise MintError(SETTING, f"{key} is missing")
    jwt = make_jwt(app_id, key, now)
    installation = _call("GET", f"/repos/{repo}/installation", jwt).get("id")
    if isinstance(installation, bool) or not isinstance(installation, int):
        raise MintError(GITHUB, f"GitHub named no installation of the App on {repo}")
    name = repo.split("/", 1)[1]
    token = _call("POST", f"/app/installations/{installation}/access_tokens", jwt, {"repositories": [name]}).get("token")
    if not isinstance(token, str) or not token:
        raise MintError(GITHUB, "GitHub's answer held no token")
    # Always resolved, whatever TRACKER_APP_LOGIN says: a caller comparing authors needs the App's real login.
    slug = settings.get("GITHUB_APP_SLUG") or None
    if slug is None:
        slug = _call("GET", "/app", jwt).get("slug")
    login = f"{slug}[bot]" if isinstance(slug, str) and re.fullmatch(r"[A-Za-z0-9-]{1,100}", slug) else None
    return token, login
