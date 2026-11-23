"""with-token: the JWT it signs, the calls it makes, and that the token reaches only the tracker's App requests."""
import sys as _sys
from pathlib import Path as _Path

# The tracker's modules sit beside each other in tools/tracker, in this source tree or a published one.
_TOOL = next(p for p in (_Path(__file__).resolve().parent.parent / "tools" / "tracker",
                         _Path(__file__).resolve().parent.parent / "plugins" / "agent-workflows" / "tools" / "tracker") if p.is_dir())
if str(_TOOL) not in _sys.path:
    _sys.path.insert(0, str(_TOOL))

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# appmint stays beside the tracker because the Backlog importer (tools/tracker-import) imports it; the tracker does not.
import appmint
from harness import HERE, REPO, FakeGh, issue

MINTED = "ghs_minted_fixture_token_value"


def b64decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class KeyFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key = Path(cls.tmp.name, "app.pem")
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)
        cls.pub = Path(cls.tmp.name, "pub.pem")
        subprocess.run(["openssl", "rsa", "-in", str(cls.key), "-pubout", "-out", str(cls.pub)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class JwtTests(KeyFixture):
    def test_the_jwt_is_rs256_with_the_app_as_issuer_and_a_short_life(self):
        token = appmint.make_jwt("12345", str(self.key), now=1_800_000_000)
        header, payload, signature = token.split(".")
        self.assertEqual(json.loads(b64decode(header)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(b64decode(payload))
        self.assertEqual(claims["iss"], "12345")
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertLess(claims["iat"], 1_800_000_000)
        sig = Path(self.tmp.name, "sig")
        sig.write_bytes(b64decode(signature))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.pub), "-signature", str(sig)],
                                  input=f"{header}.{payload}".encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_a_key_openssl_cannot_use_is_a_setting_error(self):
        with self.assertRaises(appmint.MintError) as caught:
            appmint.make_jwt("1", "/nonexistent.pem")
        self.assertEqual(caught.exception.code, 2)


class MintTests(KeyFixture):
    """The importer's minting, kept as it was."""

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.creds = Path(self.root.name, "credentials")
        self.app_dir = self.creds / "github" / "tracker-app"
        self.app_dir.mkdir(parents=True)
        (self.app_dir / "app.env").write_text("GITHUB_APP_ID=12345\n", encoding="utf-8")
        (self.app_dir / "private-key.pem").write_bytes(self.key.read_bytes())

    def env(self, **extra):
        return {"HOME": self.root.name, "TRACKER_APP_CRED": "tracker-app", "CREDENTIALS_DIR": str(self.creds), **extra}

    def test_the_default_credentials_root_is_under_the_user_config_directory(self):
        home = {"HOME": "/h", "TRACKER_APP_CRED": "my-tracker"}
        self.assertEqual(appmint.credential_dir(home), Path("/h", ".config", "tracker", "credentials", "github", "my-tracker"))
        self.assertEqual(appmint.credential_dir({**home, "XDG_CONFIG_HOME": "/x"}), Path("/x", "tracker", "credentials", "github", "my-tracker"))
        self.assertEqual(appmint.credential_dir({**home, "XDG_CONFIG_HOME": "/x", "CREDENTIALS_DIR": "/c"}),
                         Path("/c/github/my-tracker"))

    def test_mint_scopes_the_token_to_the_repository_and_resolves_the_slug(self):
        answers = [(200, {"id": 42}), (201, {"token": MINTED}), (200, {"slug": "example-tracker"})]
        calls = []

        def fake_open(request, timeout=None):
            calls.append(request)
            status, body = answers.pop(0)
            return FakeResponse(json.dumps(body).encode())

        with mock.patch.object(appmint.urllib.request, "urlopen", fake_open):
            token, login = appmint.mint(self.env(TRACKER_APP_LOGIN="other-app[bot]"), REPO)
        self.assertEqual((token, login, answers), (MINTED, "example-tracker[bot]", []))
        self.assertEqual(json.loads(calls[1].data.decode()), {"repositories": [REPO.split("/")[1]]})


class WithTokenTests(unittest.TestCase):
    """with-token is a retired name: it runs the tracker as the ambient gh login and reads no credentials."""

    def setUp(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)

    def run_with_token(self, *argv, **extra):
        env = self.gh.env(TRACKER_APP_CRED="absent-app", CREDENTIALS_DIR=str(Path(self.gh.root, "no-such-tree")),
                          TRACKER_ISSUE_IDENTITY="app", TRACKER_PROJECT_IDENTITY="user", TRACKER_PROJECT="7", **extra)
        return subprocess.run([sys.executable, "-E", "-s", str(HERE / "with-token"), *argv], env=env, capture_output=True,
                              text=True, timeout=60, check=False)

    def test_it_runs_the_tracker_without_any_credential(self):
        self.gh.route(rf"GET /repos/{REPO}/issues/5", issue(5, "Five"))
        done = self.run_with_token("view", "5")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["title"], "Five")
        self.assertEqual([c["path"] for c in self.gh.calls()], [f"/repos/{REPO}/issues/5"])
        self.assertIsNone(self.gh.calls()[0]["gh_token"])

    def test_the_tracker_exit_status_is_passed_through(self):
        self.gh.route(rf"GET /repos/{REPO}/issues/5", {"status": 500, "body": {"message": "boom"}})
        self.assertEqual(self.run_with_token("view", "5").returncode, 4)
        self.assertEqual(self.run_with_token("close", "5").returncode, 2)

    def test_it_imports_no_minting(self):
        source = (HERE / "with-token").read_text(encoding="utf-8")
        self.assertNotIn("appmint", source)
        self.assertNotIn("CREDENTIALS_DIR", source)


if __name__ == "__main__":
    unittest.main()
