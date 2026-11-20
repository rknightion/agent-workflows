"""Escaping and the PII sweep: what the tracker is allowed to put in front of GitHub."""
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

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import textsafe


class EscapeTests(unittest.TestCase):
    def test_mentions_are_wrapped_so_they_notify_nobody(self):
        self.assertEqual(textsafe.escape("ask @octocat and @org/team"), "ask `@octocat` and `@org/team`")

    def test_a_mention_after_any_non_word_character_is_wrapped(self):
        self.assertEqual(textsafe.escape("done.@octocat and @@x and (@y) and a/@z"), "done.`@octocat` and @`@x` and (`@y`) and a/`@z`")
        self.assertEqual(textsafe.escape("foo-#12 and x/#13 and (#14)"), "foo-`#12` and x/`#13` and (`#14`)")

    def test_a_backslash_before_a_reference_cannot_swallow_the_opening_backtick(self):
        self.assertEqual(textsafe.escape("\\@octocat"), "\\\\`@octocat`")
        self.assertEqual(textsafe.escape("\\#12"), "\\\\`#12`")
        self.assertEqual(textsafe.escape("\\\\@octocat"), "\\\\`@octocat`")
        self.assertEqual(textsafe.escape("\\\\\\@octocat"), "\\\\\\\\`@octocat`")
        for once in (textsafe.escape("\\@octocat"), textsafe.escape("x \\#12 y")):
            self.assertEqual(textsafe.escape(once), once)

    def test_adjacent_tokens_share_one_span_so_none_is_left_outside(self):
        self.assertEqual(textsafe.escape("#1@octocat"), "`#1@octocat`")
        self.assertEqual(textsafe.escape("@octocat@other"), "`@octocat@other`")
        self.assertEqual(textsafe.escape("#1#2 and @a#3@b"), "`#1#2` and `@a#3@b`")
        self.assertEqual(textsafe.escape("x \\#1@octocat"), "x \\\\`#1@octocat`")
        for text in ("#1@octocat", "@octocat@other", "#1#2", "@a#3@b"):
            once = textsafe.escape(text)
            self.assertEqual(textsafe.escape(once), once)
        self.assertEqual(textsafe.escape("foo@octocat@other"), "foo@octocat@other")

    def test_entity_spellings_of_at_and_hash_are_neutralised(self):
        for text in ("&#64;octocat", "&#x40;octocat", "&commat;octocat", "&#35;12", "&#x23;12", "&num;12"):
            with self.subTest(text=text):
                out = textsafe.escape(text)
                self.assertTrue(out.startswith("&amp;"), out)
                self.assertEqual(textsafe.escape(out), out)
        self.assertEqual(textsafe.escape("fish &amp; chips"), "fish &amp; chips")
        self.assertEqual(textsafe.escape("`&#64;x`"), "`&#64;x`")

    def test_gh_n_autolinks_are_wrapped_like_references(self):
        self.assertEqual(textsafe.escape("see GH-12 and gh-13"), "see `GH-12` and `gh-13`")
        self.assertEqual(textsafe.escape("xGH-12 and ABC-12"), "xGH-12 and ABC-12")
        self.assertEqual(textsafe.escape("GH-1#2@a"), "`GH-1#2@a`")

    def test_a_long_chain_of_adjacent_tokens_costs_linear_time(self):
        started = time.monotonic()
        textsafe.escape("@a" * 30000 + " " + "#1" * 30000)
        self.assertLess(time.monotonic() - started, 2)

    def test_bare_issue_references_are_wrapped_so_they_cross_link_nothing(self):
        self.assertEqual(textsafe.escape("see #12 and example-org/example#13."), "see `#12` and `example-org/example#13`.")

    def test_code_spans_and_fences_are_left_alone(self):
        text = "keep `@x #1` here\n```\n@y #2\n```\nbut @z #3"
        self.assertEqual(textsafe.escape(text), "keep `@x #1` here\n```\n@y #2\n```\nbut `@z` `#3`")

    def test_an_unpaired_backtick_does_not_hide_a_mention(self):
        self.assertEqual(textsafe.escape("odd ` then @octocat"), "odd &#96; then `@octocat`")
        self.assertEqual(textsafe.escape("odd `@octocat and #4"), "odd &#96;`@octocat` and `#4`")
        self.assertEqual(textsafe.escape("a ``` b @x"), "a &#96;&#96;&#96; b `@x`")

    def test_a_span_never_reaches_across_a_line_so_a_block_marker_cannot_hide_a_mention(self):
        text = "note `flag\n## follow-up @octocat\nand `more` text"
        self.assertEqual(textsafe.escape(text), "note &#96;flag\n## follow-up `@octocat`\nand `more` text")

    def test_only_a_closed_fence_protects_what_is_inside_it(self):
        self.assertEqual(textsafe.escape("````\n@x\n```\n@y\n````\n@z"), "````\n@x\n```\n@y\n````\n`@z`")
        self.assertEqual(textsafe.escape("~~~\n@x\n~~~\n@z"), "~~~\n@x\n~~~\n`@z`")
        self.assertEqual(textsafe.escape("```\n@x\nnever closed @z"), "&#96;&#96;&#96;\n`@x`\nnever closed `@z`")
        self.assertEqual(textsafe.escape("```py\r\n@x\r\n```\r\n@z"), "```py\r\n@x\r\n```\r\n`@z`")

    def test_a_backslash_or_a_longer_run_gives_no_protection(self):
        self.assertEqual(textsafe.escape("\\`@user`"), "\\&#96;`@user`&#96;")
        self.assertEqual(textsafe.escape("code `` a `@x` b `` then @y"), "code &#96;&#96; a `@x` b &#96;&#96; then `@y`")

    def test_many_unclosed_openers_cost_near_linear_time(self):
        started = time.monotonic()
        text = "```py\n" * 20000 + "@x"
        self.assertTrue(textsafe.escape(text).endswith("`@x`"))
        self.assertLess(time.monotonic() - started, 3)

    def test_a_shorter_opener_still_finds_the_closer_a_longer_unclosed_one_could_not(self):
        self.assertEqual(textsafe.escape("````\n@a\n```\n@b\n```\n@c"), "&#96;&#96;&#96;&#96;\n`@a`\n```\n@b\n```\n`@c`")

    def test_long_runs_before_a_qualified_reference_cost_linear_time(self):
        for run in ("-", ".", "a", "a/", "a-", "_."):
            with self.subTest(run=run):
                started = time.monotonic()
                textsafe.escape(run * 50000 + "#1")
                self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(textsafe.escape("example-org/example#13 and a.b/c-d#2"), "`example-org/example#13` and `a.b/c-d#2`")
        self.assertEqual(textsafe.escape("x.y-z/example#13"), "`x.y-z/example#13`")
        self.assertEqual(textsafe.escape("a/b/c#1"), "a/`b/c#1`")

    def test_a_huge_run_of_backslashes_costs_linear_time(self):
        started = time.monotonic()
        textsafe.escape("\\" * 60000 + " @x #4 `y`")
        self.assertLess(time.monotonic() - started, 2)

    def test_a_huge_run_of_backticks_costs_linear_time(self):
        started = time.monotonic()
        textsafe.escape("`" * 60000 + " @x")
        self.assertLess(time.monotonic() - started, 2)

    def test_escape_is_idempotent(self):
        once = textsafe.escape("@a #1 example-org/example#2 and a ` stray")
        self.assertEqual(textsafe.escape(once), once)

    def test_text_that_is_not_a_reference_is_untouched(self):
        for text in ("# Heading", "colour #fff", "mail a@example.com", "step AC#1", "## 3 items"):
            with self.subTest(text=text):
                self.assertEqual(textsafe.escape(text), text)


def ip(*octets):
    """A dotted quad built at run time, so this file holds no private-range address for the public export's leak scan."""
    return ".".join(str(octet) for octet in octets)


class SweepTests(unittest.TestCase):
    def kinds(self, text):
        return [hit.kind for hit in textsafe.sweep(text)]

    def test_identifiers_are_found_case_insensitively(self):
        for text in ("mail me@Gmail", "see HOST.TAILNET-NAME.TS" + ".NET", "token GHP_" + "a1" * 18):
            with self.subTest(text=text):
                self.assertEqual(self.kinds(text), ["identifier"])

    def test_a_real_looking_address_hits_and_documentation_ranges_do_not(self):
        self.assertEqual(self.kinds(f"host {ip(100, 101, 102, 103)} base {ip(100, 64, 0, 0)}"), ["address"])
        for ok in ("0.0.0.0", "255.255.255.255", "127.0.0.1", "169.254.0.0", ip(100, 64, 0, 0), ip(10, 0, 0, 0),
                   ip(172, 16, 0, 0), ip(192, 168, 0, 0), "192.0.2.7", "198.51.100.200", "203.0.113.9"):
            with self.subTest(address=ok):
                self.assertEqual(self.kinds(f"at {ok} now"), [])

    def test_other_private_addresses_still_hit(self):
        for bad in (ip(10, 1, 2, 3), ip(192, 168, 1, 1), ip(172, 16, 4, 4), "127.0.0.2"):
            with self.subTest(address=bad):
                self.assertEqual(self.kinds(f"at {bad} now"), ["address"])

    def test_a_hit_never_exposes_its_value_in_its_repr(self):
        hit = textsafe.sweep("me@" + "gmail.example")[0]
        self.assertNotIn("gmail", repr(hit))
        self.assertIsInstance(hit.start, int)

    def test_new_hits_ignores_what_a_baseline_already_holds(self):
        baseline = "old 8.8.4.4 here"
        self.assertEqual(textsafe.new_hits(baseline + " more", baseline), [])
        self.assertEqual([h.kind for h in textsafe.new_hits(baseline + " 8.8.4.5", baseline)], ["address"])
        self.assertEqual([h.kind for h in textsafe.new_hits(baseline + " 8.8.4.4", baseline)], ["address"])


class PatternFileTests(unittest.TestCase):
    """The identifier patterns live in one file (the shipped generic list, or TRACKER_IDENTIFIER_PATTERNS); the code holds none."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def point_at(self, content=None):
        """Aim the sweep at a temp pattern file holding content, or at a missing one when content is None."""
        path = self.directory / "patterns.txt"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        patcher = mock.patch.object(textsafe, "PATTERN_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def committed_lines(self):
        return textsafe.PATTERN_FILE.read_text(encoding="utf-8").splitlines()

    def test_the_committed_file_is_the_default_and_holds_patterns(self):
        self.assertEqual(textsafe.PATTERN_FILE.name, "identifier-patterns.txt")
        self.assertEqual(textsafe.PATTERN_FILE.parent.name, "tracker")
        self.assertTrue([line for line in self.committed_lines() if line and not line.startswith("#")])

    def test_a_line_added_to_a_copy_of_the_file_is_honoured(self):
        self.point_at("\n".join(self.committed_lines() + ["zz-sample-[0-9]+"]))
        self.assertEqual([h.kind for h in textsafe.sweep("note ZZ-Sample-42 here")], ["identifier"])
        self.assertEqual([h.kind for h in textsafe.sweep("mail me@Gmail")], ["identifier"])

    def test_a_pattern_missing_from_the_file_is_not_swept(self):
        self.point_at("zz-sample-[0-9]+\n")
        self.assertEqual(textsafe.sweep("mail me@Gmail"), [])

    def test_comments_and_empty_lines_are_not_patterns(self):
        self.point_at("# a comment mentioning zz-comment\n\nzz-real\n")
        self.assertEqual(textsafe.sweep("zz-comment and a # sign"), [])
        self.assertEqual(len(textsafe.sweep("zz-real")), 1)

    def test_a_missing_file_fails_closed(self):
        self.point_at(None)
        with self.assertRaises(textsafe.SweepUnavailable):
            textsafe.sweep("clean text")
        with self.assertRaises(textsafe.SweepUnavailable):
            textsafe.new_hits("clean text", "")

    def test_a_file_with_no_patterns_fails_closed(self):
        for content in ("", "\n\n", "# only a comment\n\n"):
            with self.subTest(content=content):
                self.point_at(content)
                with self.assertRaises(textsafe.SweepUnavailable):
                    textsafe.sweep("clean text")

    def test_an_invalid_pattern_fails_closed_without_naming_it(self):
        self.point_at("zz-ok\nzz-(unclosed\n")
        with self.assertRaises(textsafe.SweepUnavailable) as caught:
            textsafe.sweep("clean text")
        self.assertIn("line 2", str(caught.exception))
        self.assertNotIn("unclosed", str(caught.exception))

    def test_a_pattern_outside_the_subset_both_engines_read_alike_fails_closed(self):
        """A repository's own pre-commit sweep may run these lines through rg (Rust regex), so each must mean the same in Python re."""
        outside = (
            "(?<=zz)ok",       # lookbehind: rg rejects it
            "(zz)\\1",         # backreference: rg rejects it
            "zz*+",            # possessive: rg rejects it
            "zz|yy",           # alternation: split lines instead
            "\\dzz",           # shorthand classes differ in scope
            "^zz",             # anchors differ across multi-line input
            "zz.",             # an unescaped dot is a wildcard; write \\.
            "[[:alpha:]]zz",   # POSIX class: rg reads it, Python reads a set
            "[^z]zz",          # negation
            "zz(?i)",          # inline flags
            "zz\\Z",           # Python-only anchor
        )
        for line in outside:
            with self.subTest(line=line):
                self.point_at(f"zz-ok\n{line}\n")
                with self.assertRaises(textsafe.SweepUnavailable) as caught:
                    textsafe.sweep("clean text")
                self.assertIn("line 2", str(caught.exception))

    def test_the_shared_subset_is_accepted(self):
        self.point_at("zz-[0-9]{2,3}\nzz\\.example\nzz[x]+\n@zz_[a-z0-9._-]+\n")
        self.assertEqual([h.kind for h in textsafe.sweep("see ZZ-42 and zz.example")], ["identifier", "identifier"])

    @unittest.skipUnless(shutil.which("rg"), "rg is not installed")
    def test_every_committed_pattern_matches_the_same_text_in_rg_and_python(self):
        # Run-time `@`, so this file holds no address the public export's deny scan would refuse.
        samples = "me#Gmail ME#GMAIL.EXAMPLE x#Gmail.co.uk foo#gmail.c mail#gmai1 plain text".replace("#", "@")
        total = 0
        for line in self.committed_lines():
            if not line or line.startswith("#"):
                continue
            with self.subTest(line=line):
                found = subprocess.run(["rg", "-oi", "-e", line], input=samples, capture_output=True, text=True, check=False)
                self.assertIn(found.returncode, (0, 1), found.stderr)
                self.assertEqual(found.stdout.split(), [m.group(0) for m in re.finditer(line, samples, re.I)])
                total += len(found.stdout.split())
        self.assertGreater(total, 0, "no committed pattern matched the samples, so the comparison proved nothing")

    def test_each_gmail_spelling_hits_at_the_at_sign_whatever_follows_it(self):
        """The address forms the old `@gmail` alternation caught are still caught, at the same span."""
        for text in ("@gmail", "@Gmail.co.uk", "x@gmail", "foo@gmail.c", "me#GMAIL.EXAMPLE".replace("#", "@"), "`@gmail`"):
            with self.subTest(text=text):
                self.assertEqual([h.start for h in textsafe.sweep(text)], [text.index("@")])

    def test_the_committed_file_does_not_refuse_its_own_lines_quoted_as_written(self):
        """A write may quote the pattern file: each line's bracketed or escaped form never matches itself."""
        self.assertEqual(textsafe.sweep("\n".join(self.committed_lines())), [])

    def test_the_shipped_list_catches_common_secret_and_personal_data_shapes(self):
        samples = {
            "classic token": "ghp_" + "A1b2" * 9, "fine-grained token": "github_pat_" + "x" * 30, "AWS key": "AKIA" + "ABCDEFGH12345678",
            "Slack token": "xoxb-1234567890-abc", "Slack webhook": "https://hooks.slack.com/services/T0/B0/x",
            "Stripe key": "sk_live_" + "a" * 24, "Anthropic key": "sk-ant-api03-" + "a" * 30, "npm token": "npm_" + "a" * 36,
            "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.sig", "PEM block": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "URL credentials": "https://user:hunter2@example.com/x", "Tailscale key": "tskey-auth-abcdef123456",
            # These two are spelled in parts so this file holds no address or tailnet name the export's deny scan would refuse.
            "consumer mailbox": "someone@" + "outlook.com", "tailnet name": "box.tail1234.ts" + ".net",
        }
        for kind, text in samples.items():
            with self.subTest(kind=kind):
                self.assertEqual([h.kind for h in textsafe.sweep(f"see {text} here")], ["identifier"])
        for clean in ("git@github.com:owner/repo.git", "noreply@example.com", "https://github.com/owner/repo/issues/5",
                      "ghp_ is a token prefix", "a work address like dev@example.com"):
            with self.subTest(clean=clean):
                self.assertEqual(textsafe.sweep(clean), [])

    def test_the_setting_replaces_the_shipped_list_and_fails_closed_when_it_cannot_be_read(self):
        path = self.directory / "mine.txt"
        path.write_text("zz-mine\n", encoding="utf-8")
        self.addCleanup(textsafe.configure, None)
        textsafe.configure(str(path))
        self.assertEqual(len(textsafe.sweep("zz-mine")), 1)
        self.assertEqual(textsafe.sweep("mail me@Gmail"), [])  # the override replaces the shipped list
        textsafe.configure(str(self.directory / "missing.txt"))
        with self.assertRaises(textsafe.SweepUnavailable):
            textsafe.sweep("clean text")
        textsafe.configure(None)
        self.assertEqual(len(textsafe.sweep("mail me@Gmail")), 1)

    def test_a_file_that_is_not_utf8_fails_closed(self):
        path = self.point_at(None)
        path.write_bytes(b"\xff\xfe\x00zz")
        with self.assertRaises(textsafe.SweepUnavailable):
            textsafe.sweep("clean text")


if __name__ == "__main__":
    unittest.main()
