"""with-token: the JWT it signs, the calls it makes, and that the token reaches only the tracker's App requests."""
import sys as _sys
from pathlib import Path as _Path
import unittest as _unittest

# The tracker needs Python 3.12 or newer. Skip the whole module before any tracker module is imported, so an older
# interpreter (the rest of this tree supports 3.9) reports a skip, not an import error.
if _sys.version_info < (3, 12):
    raise _unittest.SkipTest("the tracker needs Python 3.12 or newer")

# The tracker's modules sit beside each other in tools/tracker, in this source tree or a published one.
_TOOL = next(p for p in (_Path(__file__).resolve().parent.parent / "tools" / "tracker",
                         _Path(__file__).resolve().parent.parent / "plugins" / "agent-workflows" / "tools" / "tracker") if p.is_dir())
if str(_TOOL) not in _sys.path:
    _sys.path.insert(0, str(_TOOL))

import base64
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import HERE, REPO, FakeGh, comment, issue

loader = importlib.machinery.SourceFileLoader("tracker_with_token", str(HERE / "with-token"))
spec = importlib.util.spec_from_loader("tracker_with_token", loader)
wt = importlib.util.module_from_spec(spec)
loader.exec_module(wt)

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
        token = wt.make_jwt("12345", str(self.key), now=1_800_000_000)
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
        with self.assertRaises(wt.MintError) as caught:
            wt.make_jwt("1", "/nonexistent.pem")
        self.assertEqual(caught.exception.code, 2)


class WithTokenTests(KeyFixture):
    def setUp(self):
        self.gh = FakeGh()
        self.addCleanup(self.gh.cleanup)
        self.creds = Path(self.gh.root, "credentials")
        self.app_dir = self.creds / "github" / "tracker-app"
        self.app_dir.mkdir(parents=True)
        (self.app_dir / "app.env").write_text("# the App\nexport GITHUB_APP_ID=12345\nGITHUB_APP_SLUG='example-tracker'\nGITHUB_APP_OWNER=x\n",
                                              encoding="utf-8")
        (self.app_dir / "private-key.pem").write_bytes(self.key.read_bytes())

    def env(self, **extra):
        env = self.gh.env(app_token=None, TRACKER_APP_CRED="tracker-app", CREDENTIALS_DIR=str(self.creds))
        env.pop("TRACKER_APP_LOGIN")
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run_main(self, argv, answers, env=None, cwd=None):
        calls = []

        def fake_open(request, timeout=None):
            calls.append(request)
            status, body = answers.pop(0)
            if status >= 400:
                raise wt.appmint.urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))
            return FakeResponse(json.dumps(body).encode())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wt.appmint.urllib.request, "urlopen", fake_open):
            code = wt.main(argv, env=env or self.env(), stdin=io.StringIO(""), out=out, err=err, cwd=cwd or self.gh.root)
        return code, out.getvalue(), err.getvalue(), calls

    def test_the_default_credentials_root_is_under_the_user_config_directory(self):
        home = {"HOME": "/h", "TRACKER_APP_CRED": "my-tracker"}
        self.assertEqual(wt.appmint.credential_dir(home), Path("/h", ".config", "tracker", "credentials", "github", "my-tracker"))
        self.assertEqual(wt.appmint.credential_dir({**home, "XDG_CONFIG_HOME": "/x"}), Path("/x", "tracker", "credentials", "github", "my-tracker"))
        self.assertEqual(wt.appmint.credential_dir({**home, "XDG_CONFIG_HOME": "/x", "CREDENTIALS_DIR": "/c"}),
                         Path("/c/github/my-tracker"))

    def test_it_mints_for_the_repository_and_the_token_reaches_only_app_requests(self):
        self.gh.route(rf"GET /repos/{REPO}/issues/5/comments\?per_page=100&page=1", [])
        self.gh.route(rf"POST /repos/{REPO}/issues/5/comments", {"status": 201, "body": comment(9, "x", login="example-tracker[bot]")})
        before = dict(os.environ)
        code, out, err, calls = self.run_main(["status", "5", "--text", "working on it"],
                                              [(200, {"id": 42}), (201, {"token": MINTED, "expires_at": "x"})])
        self.assertEqual(code, 0, err)
        self.assertEqual([c.full_url for c in calls], [f"https://api.github.com/repos/{REPO}/installation",
                                                       "https://api.github.com/app/installations/42/access_tokens"])
        self.assertEqual(calls[1].get_method(), "POST")
        self.assertTrue(calls[0].get_header("Authorization").startswith("Bearer "))
        self.assertEqual(json.loads(out)["action"], "created")
        gh_calls = self.gh.calls()
        app_calls = [c for c in gh_calls if c["path"] in ("/markdown", f"/repos/{REPO}/issues/5/comments") and c["method"] == "POST"]
        self.assertTrue(app_calls)
        for call in gh_calls:
            self.assertNotIn(MINTED, json.dumps(call["argv"]))
            self.assertIsNone(call["tracker_app_token"])
            self.assertEqual(call["gh_token"] == MINTED, call in app_calls, call["path"])
        self.assertNotIn(MINTED, out + err)
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn("TRACKER_APP_TOKEN", os.environ)
        audit = (self.gh.state / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(MINTED, audit)
        self.assertNotIn(calls[0].get_header("Authorization").split()[1], out + err + audit)

    def test_the_app_login_comes_from_the_slug_and_an_explicit_one_wins(self):
        self.gh.route(rf"GET /repos/{REPO}/issues/5/comments\?per_page=100&page=1", [comment(9, "x", login="example-tracker[bot]", kind="Bot")])
        code, _, err, _ = self.run_main(["status", "5", "--text", "t"], [(200, {"id": 42}), (201, {"token": MINTED})])
        self.assertEqual(code, 3, err)  # the slug's bot login owns that comment, so it is a stray App comment
        self.assertIn("App comment", err)
        self.gh.route(rf"POST /repos/{REPO}/issues/5/comments", {"status": 201, "body": comment(10, "y")})
        code, _, err, _ = self.run_main(["status", "5", "--text", "t"], [(200, {"id": 42}), (201, {"token": MINTED})],
                                        env=self.env(TRACKER_APP_LOGIN="other-app[bot]"))
        self.assertEqual(code, 0, err)

    def test_without_a_slug_the_login_is_read_from_the_app(self):
        (self.app_dir / "app.env").write_text("GITHUB_APP_ID=12345\n", encoding="utf-8")
        self.gh.route(rf"GET /repos/{REPO}/issues/5", issue(5))
        code, _, err, calls = self.run_main(["view", "5"], [(200, {"id": 42}), (201, {"token": MINTED}), (200, {"slug": "example-tracker"})])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[2].full_url, "https://api.github.com/app")

    def test_mint_always_resolves_the_slug_even_with_an_explicit_login(self):
        (self.app_dir / "app.env").write_text("GITHUB_APP_ID=12345\n", encoding="utf-8")
        answers = [(200, {"id": 42}), (201, {"token": MINTED}), (200, {"slug": "example-tracker"})]

        def fake_open(request, timeout=None):
            status, body = answers.pop(0)
            return FakeResponse(json.dumps(body).encode())

        with mock.patch.object(wt.appmint.urllib.request, "urlopen", fake_open):
            token, login = wt.appmint.mint(self.env(TRACKER_APP_LOGIN="other-app[bot]"), REPO)
        self.assertEqual((token, login, answers), (MINTED, "example-tracker[bot]", []))

    def test_the_repository_comes_from_the_origin_remote_when_unset(self):
        checkout = Path(self.gh.root, "checkout")
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "git@github.com:other-owner/other-repo.git"], check=True)
        self.gh.route(r"GET /repos/other-owner/other-repo/issues/5", issue(5))
        code, _, err, calls = self.run_main(["view", "5"], [(200, {"id": 42}), (201, {"token": MINTED})],
                                            env=self.env(TRACKER_REPO=None), cwd=str(checkout))
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0].full_url, "https://api.github.com/repos/other-owner/other-repo/installation")

    def test_settings_problems_exit_2_before_any_call(self):
        cases = {
            "no credential name": self.env(TRACKER_APP_CRED=None),
            "a path for a name": self.env(TRACKER_APP_CRED="../elsewhere"),
            "a missing directory": self.env(TRACKER_APP_CRED="absent"),
        }
        for label, env in cases.items():
            with self.subTest(label):
                code, out, err, calls = self.run_main(["view", "5"], [], env=env)
                self.assertEqual((code, out, calls), (2, "", []), err)
        (self.app_dir / "app.env").write_text("GITHUB_APP_SLUG=x\n", encoding="utf-8")
        code, _, err, calls = self.run_main(["view", "5"], [])
        self.assertEqual((code, calls), (2, []))
        self.assertIn("GITHUB_APP_ID", err)
        code, _, _, calls = self.run_main(["view", "5"], [], env=self.env(TRACKER_REPO="not-a-repo"))
        self.assertEqual((code, calls), (2, []))
        self.assertEqual(self.gh.calls(), [])

    def test_a_github_error_exits_4_and_shows_neither_jwt_nor_token(self):
        code, out, err, calls = self.run_main(["view", "5"], [(404, {"message": "Not Found"})])
        self.assertEqual((code, out), (4, ""))
        self.assertIn("Not Found", err)
        self.assertNotIn(calls[0].get_header("Authorization").split()[1], err)
        self.assertEqual(self.gh.calls(), [])

    def test_an_answer_without_a_token_or_installation_fails_without_printing_the_body(self):
        for answers in ([(200, {"unexpected": 1})], [(200, {"id": 42}), (201, {"unexpected": 1})], [(200, {"id": 42}), (500, ["x"])]):
            with self.subTest(answers=answers):
                code, out, err, _ = self.run_main(["view", "5"], list(answers))
                self.assertEqual((code, out), (4, ""))
                self.assertNotIn("unexpected", err)
        self.assertEqual(self.gh.calls(), [])

    def test_the_token_is_scoped_to_the_one_repository_by_name(self):
        self.gh.route(rf"GET /repos/{REPO}/issues/5", issue(5))
        code, _, err, calls = self.run_main(["view", "5"], [(200, {"id": 42}), (201, {"token": MINTED})])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[1].get_method(), "POST")
        self.assertEqual(json.loads(calls[1].data.decode()), {"repositories": [REPO.split("/")[1]]})
        self.assertNotIn("permissions", json.loads(calls[1].data.decode()))

    def test_there_is_no_mode_that_prints_a_token(self):
        for argv in (["--print-token"], ["--print-token", "--repo", REPO]):
            with self.subTest(argv=argv):
                code, out, _, calls = self.run_main(argv, [(200, {"id": 42}), (201, {"token": MINTED})])
                self.assertEqual((code, out, calls), (2, "", []))

    def test_the_tracker_exit_status_is_passed_through(self):
        self.gh.route(rf"GET /repos/{REPO}/issues/5", {"status": 500, "body": {"message": "boom"}})
        code, _, _, _ = self.run_main(["view", "5"], [(200, {"id": 42}), (201, {"token": MINTED})])
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
