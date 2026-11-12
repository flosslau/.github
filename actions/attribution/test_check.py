# Copyright 2026 Bruno Venceslau
# SPDX-License-Identifier: Apache-2.0
"""Tests of check.py. Run: python3 -m unittest discover -s actions/attribution

Every commit these tests judge is made in a throwaway repository; nothing is
pushed anywhere.
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import check

RULES = check.load_rules()
HUMAN = ("Ada Lovelace", "ada@example.org")

# The author and committer lines of a commit written by hand (Repo.raw_commit).
RAW_AUTHOR = b"author Ada <ada@example.org> 1700000000 +0000\n"
RAW_COMMITTER = b"committer Ada <ada@example.org> 1700000000 +0000\n"

# What rules.toml must list, spelled out here so that a rule dropped or changed
# there fails a test. Add a rule to rules.toml and to this table together.
EXPECTED_RULES = {
    ("Claude Code", "email", "noreply@anthropic.com"),
    ("Claude Code", "login", "claude[bot]"),
    ("Claude Code", "line", r"^Claude-Session:\s"),
    ("Claude Code", "line", r"^([^\x00-\x7f]+\s)?Generated with \[Claude Code\]\("),
    ("GitHub Copilot", "email", "copilot@github.com"),
    ("GitHub Copilot", "login", "copilot"),
    ("GitHub Copilot", "login", "copilot-swe-agent[bot]"),
    ("Cursor", "email", "cursoragent@cursor.com"),
    ("Cursor", "login", "cursor[bot]"),
    ("OpenAI Codex", "email", "codex@openai.com"),
    ("OpenAI Codex", "email", "noreply@openai.com"),
    ("OpenAI Codex", "login", "chatgpt-codex-connector[bot]"),
    ("OpenAI Codex", "login", "openai-codex[bot]"),
    ("Amp", "email", "amp@ampcode.com"),
    ("Amp", "line", r"^Amp-Thread-ID:\s"),
    ("Aider", "email", "aider@aider.chat"),
    ("Aider", "suffix", " (aider)"),
    ("Junie", "email", "junie@jetbrains.com"),
    ("OpenCode", "email", "noreply@opencode.ai"),
    ("OpenCode", "login", "opencode-agent[bot]"),
    ("Warp", "email", "agent@warp.dev"),
    ("Devin", "login", "devin-ai-integration[bot]"),
    ("Jules", "login", "google-labs-jules[bot]"),
    ("Gemini CLI", "login", "gemini-cli[bot]"),
    ("Factory Droid", "login", "factory-droid[bot]"),
}

# One line each pattern of rules.toml must refuse, written out by hand.
EXAMPLE_LINES = [
    ("Claude Code", "Claude-Session: https://claude.ai/code/abc"),
    ("Claude Code", "Generated with [Claude Code](https://claude.com/claude-code)"),
    ("Claude Code", "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"),
    ("Amp", "Amp-Thread-ID: https://ampcode.com/threads/T-1"),
]


class Repo:
    """A throwaway repository with one base commit by a human."""

    def __init__(self, root: Path, object_format: str = "sha1"):
        self.dir = str(root / "repo")
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        os.mkdir(self.dir)
        self.git("init", "-q", "-b", "main", f"--object-format={object_format}")
        self.base = self.commit("base")

    def reset(self) -> None:
        """Drop every commit made since the base."""
        self.git("reset", "-q", "--hard", self.base)

    def raw_commit(self, headers: bytes, message: bytes) -> str:
        """Write a commit object byte for byte, as git would not let `git commit`.

        headers holds the author, committer and any other header lines, each
        ending in a newline. The commit goes on top of HEAD, and HEAD moves to it.
        """
        tree = self.git("rev-parse", "HEAD^{tree}").encode()
        parent = self.git("rev-parse", "HEAD").encode()
        raw = b"tree " + tree + b"\nparent " + parent + b"\n" + headers + b"\n" + message
        sha = self.git("hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=raw)
        self.git("update-ref", "HEAD", sha)
        return sha

    def git(self, *args: str, env: dict | None = None, stdin: bytes | None = None) -> str:
        out = subprocess.run(
            ["git", "-C", self.dir, *args],
            check=True,
            capture_output=True,
            input=stdin,
            env={**self.env, **(env or {})},
        )
        return out.stdout.decode().strip()

    def commit(self, message, author=HUMAN, committer=HUMAN) -> str:
        if isinstance(message, str):
            message = message.encode()
        env = {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }
        self.git("commit", "-q", "--allow-empty", "-F", "-", env=env, stdin=message)
        return self.git("rev-parse", "HEAD")


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = Repo(self.root)

    def run_check(
        self, *, title="Add a thing", body="", login="ada", event=None, source="--repo"
    ):
        """Run main() on the commits made since the base; return (status, output)."""
        if event is None:
            event = {
                "pull_request": {
                    "title": title,
                    "body": body,
                    "user": {"login": login},
                    "base": {"sha": self.repo.base},
                    "head": {"sha": self.repo.git("rev-parse", "HEAD")},
                }
            }
        path = self.root / "event.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = check.main([source, self.repo.dir, "--event", str(path)])
        return status, out.getvalue()


class Refused(Case):
    def refused(self, needle, **kwargs):
        status, out = self.run_check(**kwargs)
        self.assertEqual(status, 1, out)
        self.assertIn(needle, out)
        self.assertIn("How to fix it", out)
        return out

    def test_agent_author(self):
        self.repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        self.refused("author: e-mail noreply@anthropic.com (Claude Code)")

    def test_agent_committer(self):
        self.repo.commit("feat: x", committer=("Cursor Agent", "cursoragent@cursor.com"))
        self.refused("committer: e-mail cursoragent@cursor.com (Cursor)")

    def test_email_case_is_ignored(self):
        self.repo.commit("feat: x", author=("Claude", "NoReply@Anthropic.com"))
        self.refused("(Claude Code)")

    def test_bot_noreply_address_with_and_without_id(self):
        for email in (
            "198982749+Copilot@users.noreply.github.com",
            "copilot-swe-agent[bot]@users.noreply.github.com",
        ):
            with self.subTest(email=email):
                self.repo.reset()
                self.repo.commit("feat: x", author=("Copilot", email))
                self.refused("(GitHub Copilot)")

    def test_name_suffix(self):
        self.repo.commit("feat: x", author=("Ada Lovelace (aider)", HUMAN[1]))
        self.refused("author: name Ada Lovelace (aider) (Aider)")

    def test_co_author_trailer(self):
        self.repo.commit(
            "feat: x\n\nBody.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>\n"
        )
        self.refused("message, line 5: Co-Authored-By: Claude Opus 4.8")

    def test_trailer_key_and_case_do_not_matter(self):
        self.repo.commit("feat: x\n\nsigned-off-by: Bot <AIDER@aider.chat>\n")
        self.refused("(Aider)")

    def test_session_trailer_and_footer(self):
        for message, needle in (
            ("feat: x\n\nClaude-Session: https://claude.ai/code/abc\n", "Claude-Session:"),
            (
                "feat: y\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n",
                "Generated with [Claude Code]",
            ),
        ):
            with self.subTest(needle=needle):
                self.repo.reset()  # each message must be refused on its own
                self.repo.commit(message)
                self.refused(needle)

    def test_crlf_message(self):
        self.repo.commit("feat: x\r\n\r\nAmp-Thread-ID: https://ampcode.com/t/1\r\n")
        self.refused("(Amp)")

    def test_message_that_is_not_utf8(self):
        # `git commit` re-encodes a Latin-1 message to UTF-8, so write the object by hand.
        self.repo.raw_commit(
            RAW_AUTHOR + RAW_COMMITTER,
            b"feat: caf\xe9\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n",
        )
        self.refused("(Cursor)")

    def test_author_name_that_is_not_utf8_and_an_encoding_header(self):
        self.repo.raw_commit(
            b"author Jos\xe9 <noreply@anthropic.com> 1700000000 +0000\n"
            + RAW_COMMITTER
            + b"encoding ISO-8859-1\n",
            b"feat: caf\xe9\n",
        )
        self.refused("author: e-mail noreply@anthropic.com (Claude Code)")

    def test_signed_commit_is_read_through_its_gpgsig_header(self):
        self.repo.raw_commit(
            RAW_AUTHOR
            + b"committer Claude <noreply@anthropic.com> 1700000000 +0000\n"
            + b"gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n =abcd\n"
            + b" -----END PGP SIGNATURE-----\n",
            b"feat: signed\n",
        )
        self.refused("committer: e-mail noreply@anthropic.com (Claude Code)")

    def test_agent_trailer_in_the_message_of_a_signed_commit(self):
        self.repo.raw_commit(
            RAW_AUTHOR
            + RAW_COMMITTER
            + b"gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n"
            + b" -----END PGP SIGNATURE-----\n",
            b"feat: signed\n\nCo-authored-by: Claude <noreply@anthropic.com>\n",
        )
        self.refused("message, line 3:")

    def test_merge_commit_made_by_an_agent(self):
        self.repo.git("switch", "-q", "-c", "side")
        self.repo.commit("feat: side")
        self.repo.git("switch", "-q", "main")
        self.repo.commit("feat: main")
        self.repo.git(
            "merge", "-q", "--no-ff", "-m", "Merge side", "side",
            env={
                "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org",
                "GIT_COMMITTER_NAME": "Claude", "GIT_COMMITTER_EMAIL": "noreply@anthropic.com",
            },
        )
        self.refused("committer: e-mail noreply@anthropic.com (Claude Code)")

    def test_agent_commit_on_the_second_parent_of_a_merge(self):
        self.repo.git("switch", "-q", "-c", "side")
        self.repo.commit("feat: side", author=("Claude", "noreply@anthropic.com"))
        self.repo.git("switch", "-q", "main")
        self.repo.commit("feat: main")
        self.repo.git(
            "merge", "-q", "--no-ff", "-m", "Merge side", "side",
            env={
                "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org",
                "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.org",
            },
        )
        self.refused("author: e-mail noreply@anthropic.com (Claude Code)")

    def test_trailer_with_a_tab_after_the_key_or_spaces_after_the_address(self):
        for line in (
            "Co-authored-by:\tClaude <noreply@anthropic.com>",
            "Co-authored-by: Claude <noreply@anthropic.com>  \t ",
        ):
            with self.subTest(line=line):
                self.repo.reset()
                # `git commit` strips trailing spaces, so write the object by hand.
                self.repo.raw_commit(RAW_AUTHOR + RAW_COMMITTER, f"feat: x\n\n{line}\n".encode())
                self.refused("(Claude Code)")

    def test_crlf_trailer_in_a_commit_and_in_a_description(self):
        trailer = "Co-authored-by: Claude <noreply@anthropic.com>"
        self.repo.commit(f"feat: x\r\n\r\n{trailer}\r\n")
        self.refused("message, line 3:")
        self.repo.reset()
        self.repo.commit("feat: clean")
        self.refused("pull request description, line 3:", body=f"Summary.\r\n\r\n{trailer}\r\n")

    def test_every_finding_of_a_text_is_reported(self):
        self.repo.commit("feat: x")
        out = self.refused(
            "pull request description, line 1:",
            body="Amp-Thread-ID: a\nfiller\nAmp-Thread-ID: b",
        )
        self.assertIn("pull request description, line 3:", out)

    def test_name_and_e_mail_of_one_identity_are_both_reported(self):
        self.repo.commit("feat: x", author=("Ada (aider)", "aider@aider.chat"))
        out = self.refused("author: e-mail aider@aider.chat (Aider)")
        self.assertIn("author: name Ada (aider) (Aider)", out)

    def test_name_suffix_on_the_committer(self):
        self.repo.commit("feat: x", committer=("Ada (aider)", HUMAN[1]))
        self.refused("committer: name Ada (aider) (Aider)")

    def test_commit_is_named_by_its_first_seven_characters(self):
        sha = self.repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        out = self.refused(f"commit {sha[:7]}, author")
        self.assertNotIn(f"commit {sha[:8]}", out)

    def test_pull_request_opened_by_a_bot_in_the_case_github_reports(self):
        self.repo.commit("feat: x")
        self.refused("(GitHub Copilot)", login="Copilot")
        self.refused("(Devin)", login="Devin-AI-Integration[Bot]")

    def test_empty_range_still_judges_the_pull_request_text(self):
        self.refused("pull request title, line 1:", title="Amp-Thread-ID: x")

    def test_sha256_commit_ids_are_accepted(self):
        root = self.root / "sha256"
        root.mkdir()
        try:
            repo = Repo(root, object_format="sha256")
        except subprocess.CalledProcessError:
            self.skipTest("this git cannot make a sha256 repository")
        repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        self.repo = repo
        head = repo.git("rev-parse", "HEAD")
        self.assertEqual(len(head), 64)
        self.refused("author: e-mail noreply@anthropic.com (Claude Code)")

    def test_long_line_is_cut_in_the_output(self):
        self.repo.commit("feat: x\n\nAmp-Thread-ID: " + "x" * 100_000 + "\n")
        out = self.refused("(Amp)")
        self.assertLess(max(len(line) for line in out.splitlines()), 300)

    def test_description_of_thousands_of_lines_is_read_to_the_end(self):
        self.repo.commit("feat: x")
        body = "\n".join(["filler"] * 20_000 + ["Amp-Thread-ID: x"])
        self.refused("pull request description, line 20001:", body=body)

    def test_line_built_to_stall_the_trailer_pattern_does_not_hang(self):
        self.repo.commit("feat: x")
        started = time.monotonic()
        status, out = self.run_check(body="Co-authored-by: " + "<a" * 50_000)
        self.assertEqual(status, 0, out)
        self.assertLess(time.monotonic() - started, 10)

    def test_every_address_of_an_identity_line_is_read(self):
        # Readers disagree on which address counts when a line holds two.
        for author in (
            b"author x <noreply@anthropic.com> <ada@example.org> 1700000000 +0000\n",
            b"author x <ada@example.org> <noreply@anthropic.com> 1700000000 +0000\n",
        ):
            with self.subTest(author=author):
                self.repo.reset()
                self.repo.raw_commit(author + RAW_COMMITTER, b"feat: x\n")
                self.refused("author: e-mail noreply@anthropic.com (Claude Code)")

    def test_long_list_of_findings_is_cut(self):
        self.repo.commit("feat: x")
        out = self.refused("pull request description, line 1:", body="Amp-Thread-ID: x\n" * 500)
        self.assertIn("500 finding(s)", out)
        self.assertIn("... and 450 more", out)
        self.assertNotIn("line 51:", out)

    def test_commits_fetched_from_a_remote(self):
        self.repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        self.refused("author: e-mail noreply@anthropic.com (Claude Code)", source="--remote")

    def test_replace_ref_does_not_hide_a_commit(self):
        clean = self.repo.commit("feat: clean")
        self.repo.reset()
        dirty = self.repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        self.repo.git("replace", dirty, clean)
        self.refused("author: e-mail noreply@anthropic.com (Claude Code)")

    def test_every_commit_of_the_range_is_read(self):
        self.repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        self.repo.commit("feat: clean on top")
        self.refused("(Claude Code)")

    def test_mailmap_does_not_hide_the_author(self):
        Path(self.repo.dir, ".mailmap").write_text(
            "Ada Lovelace <ada@example.org> Claude <noreply@anthropic.com>\n"
        )
        self.repo.git("add", ".mailmap")
        self.repo.commit("feat: x", author=("Claude", "noreply@anthropic.com"))
        self.refused("author: e-mail noreply@anthropic.com")

    def test_pull_request_title_and_description(self):
        self.repo.commit("feat: x")
        self.refused(
            "pull request description, line 3:",
            body="Summary.\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)",
        )
        self.refused(
            "pull request title, line 1:",
            title="Co-authored-by: Codex <codex@openai.com>",
        )

    def test_pull_request_opened_by_a_bot(self):
        self.repo.commit("feat: x")
        self.refused("opened by devin-ai-integration[bot] (Devin)", login="devin-ai-integration[bot]")

    def test_output_cannot_start_a_workflow_command(self):
        self.repo.commit("feat: x\n\nAmp-Thread-ID: ::error::owned\x1b[0m\n")
        out = self.refused("(Amp)")
        self.assertNotIn("::error::", out)
        self.assertNotIn("\x1b", out)


class Accepted(Case):
    def accepted(self, **kwargs):
        status, out = self.run_check(**kwargs)
        self.assertEqual(status, 0, out)

    def test_human_commit(self):
        self.repo.commit("feat: x\n\nCo-authored-by: Grace Hopper <grace@example.org>\n")
        self.accepted()

    def test_a_person_called_claude(self):
        self.repo.commit(
            "feat: x\n\nCo-authored-by: Claude Shannon <claude@example.org>\n",
            author=("Claude Shannon", "claude@example.org"),
        )
        self.accepted(login="claude")

    def test_a_message_that_only_mentions_a_tool(self):
        self.repo.commit(
            "chore: remove the tracked .claude directory\n\n"
            "It turned off the attribution of Claude Code and its session link.\n"
            "The address noreply@anthropic.com is refused by the check.\n"
        )
        self.accepted(body="Mentions Claude Code, Copilot and Cursor in prose.")

    def test_an_indented_or_quoted_example_line(self):
        self.repo.commit(
            "docs: explain the rule\n\n"
            "    Co-Authored-By: Claude <noreply@anthropic.com>\n"
            "> Claude-Session: https://claude.ai/code/abc\n"
            "The line `Amp-Thread-ID: x` is refused.\n"
        )
        self.accepted(body="- `Co-authored-by: Cursor <cursoragent@cursor.com>`")

    def test_dependency_bots(self):
        for login in ("dependabot[bot]", "renovate[bot]", "github-actions[bot]"):
            with self.subTest(login=login):
                email = f"12345+{login}@users.noreply.github.com"
                self.repo.commit(
                    f"build: bump x\n\nSigned-off-by: {login} <{email}>\n",
                    author=(login, email),
                )
                self.accepted(login=login)

    def test_commits_before_the_base_are_not_read(self):
        self.repo.commit("feat: old", author=("Claude", "noreply@anthropic.com"))
        self.repo.base = self.repo.git("rev-parse", "HEAD")
        self.repo.commit("feat: new")
        self.accepted()

    def test_description_absent(self):
        self.repo.commit("feat: x")
        self.accepted(body=None)

    def test_signed_human_commit(self):
        self.repo.raw_commit(
            RAW_AUTHOR
            + RAW_COMMITTER
            + b"gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n"
            + b" -----END PGP SIGNATURE-----\n",
            b"feat: signed\n",
        )
        self.accepted()

    def test_merge_commit_of_humans(self):
        self.repo.git("switch", "-q", "-c", "side")
        self.repo.commit("feat: side")
        self.repo.git("switch", "-q", "main")
        self.repo.commit("feat: main")
        self.repo.git(
            "merge", "-q", "--no-ff", "-m", "Merge side", "side",
            env={
                "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org",
                "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.org",
            },
        )
        self.accepted()

    def test_empty_range_with_a_clean_title_and_description(self):
        self.accepted(title="Add a thing", body="Summary.")

    def test_title_description_and_user_that_are_null(self):
        self.repo.commit("feat: x")
        head = self.repo.git("rev-parse", "HEAD")
        event = {
            "pull_request": {
                "title": None,
                "body": None,
                "user": None,
                "base": {"sha": self.repo.base},
                "head": {"sha": head},
            }
        }
        self.accepted(event=event)

    def test_name_that_only_contains_a_suffix_rule(self):
        self.repo.commit("feat: x", author=("Ada (aider) Lovelace", HUMAN[1]))
        self.accepted()

    def test_address_that_is_a_bot_login_on_another_host(self):
        for email in ("copilot@example.org", "devin-ai-integration[bot]@example.org"):
            with self.subTest(email=email):
                self.repo.reset()
                self.repo.commit(
                    f"feat: x\n\nCo-authored-by: Someone <{email}>\n",
                    author=("Someone", email),
                    committer=("Someone", email),
                )
                self.accepted()

    def test_clean_commits_fetched_from_a_remote(self):
        self.repo.commit("feat: old", author=("Claude", "noreply@anthropic.com"))
        self.repo.base = self.repo.git("rev-parse", "HEAD")
        self.repo.commit("feat: new")
        self.accepted(source="--remote")

    def test_footer_text_in_a_list(self):
        self.repo.commit("feat: x")
        self.accepted(
            body=(
                "- Generated with [Claude Code](https://claude.com/claude-code)\n"
                "* Generated with [Claude Code](https://claude.com/claude-code)\n"
                "1. Generated with [Claude Code](https://claude.com/claude-code)\n"
            )
        )

    def test_footer_text_that_is_quoted_indented_or_in_prose(self):
        self.repo.commit("feat: x")
        self.accepted(
            body=(
                "> \U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
                "    Generated with [Claude Code](https://claude.com/claude-code)\n"
                "We drop the Generated with [Claude Code](https://claude.com/claude-code) footer.\n"
            )
        )


class CannotJudge(Case):
    def cannot_judge(self, needle, **kwargs):
        status, out = self.run_check(**kwargs)
        self.assertEqual(status, 2, out)
        self.assertIn(needle, out)

    def test_not_a_pull_request_event(self):
        self.cannot_judge("not a pull request event", event={"ref": "refs/heads/main"})

    def test_event_without_commit_ids(self):
        self.cannot_judge("lacks", event={"pull_request": {"title": "x"}})

    def test_commit_id_that_is_not_one(self):
        event = {"pull_request": {"base": {"sha": "--all"}, "head": {"sha": self.repo.base}}}
        self.cannot_judge("not a commit id", event=event)

    def test_commit_missing_from_the_clone(self):
        event = {"pull_request": {"base": {"sha": self.repo.base}, "head": {"sha": "0" * 40}}}
        self.cannot_judge("git rev-list failed", event=event)

    def test_event_file_that_is_not_json(self):
        path = self.root / "bad.json"
        path.write_text("{")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = check.main(["--repo", self.repo.dir, "--event", str(path)])
        self.assertEqual(status, 2, out.getvalue())

    def test_commit_whose_author_or_committer_cannot_be_read(self):
        for name, headers in (
            ("no committer", RAW_AUTHOR),
            ("no author", RAW_COMMITTER),
            ("text after the date", b"author Ada <ada@example.org> 1700000000 +0000 x\n" + RAW_COMMITTER),
            ("no address", b"author Ada 1700000000 +0000\n" + RAW_COMMITTER),
        ):
            with self.subTest(case=name):
                self.repo.reset()
                self.repo.raw_commit(headers, b"feat: x\n")
                self.cannot_judge("author or committer not readable")

    def test_ids_that_are_not_commit_ids_in_either_place(self):
        good = self.repo.base
        for bad in (
            "", "HEAD", "main", "--all", "g" * 40, "A" * 40, "a" * 39, "a" * 41,
            "a" * 63, "a" * 65, " " + "a" * 40, "a" * 40 + " ",
        ):
            for place in ("base", "head"):
                with self.subTest(place=place, bad=bad):
                    ids = {"base": good, "head": good, place: bad}
                    event = {"pull_request": {"base": {"sha": ids["base"]}, "head": {"sha": ids["head"]}}}
                    self.cannot_judge("not a commit id", event=event)

    def test_event_with_a_null_base(self):
        event = {"pull_request": {"base": None, "head": {"sha": self.repo.base}}}
        self.cannot_judge("lacks", event=event)

    def test_event_file_that_does_not_exist(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = check.main(["--repo", self.repo.dir, "--event", str(self.root / "none.json")])
        self.assertEqual(status, 2, out.getvalue())

    def test_event_with_values_of_the_wrong_type(self):
        ok = {"base": {"sha": self.repo.base}, "head": {"sha": self.repo.base}}
        for name, event in {
            "event is a list": [],
            "title is a number": {"pull_request": {**ok, "title": 5}},
            "description is a list": {"pull_request": {**ok, "body": ["x"]}},
            "user is a string": {"pull_request": {**ok, "user": "x"}},
            "commit id is a number": {"pull_request": {**ok, "base": {"sha": 5}}},
        }.items():
            with self.subTest(name):
                self.cannot_judge("cannot judge", event=event)

    def test_rules_file_that_is_broken(self):
        self.repo.commit("feat: x")
        broken = self.root / "rules.toml"
        for name, text in {
            "no name": '[[agent]]\nemails = ["a@b.c"]\n',
            "bad pattern": '[[agent]]\nname = "X"\nlines = ["^("]\n',
            "not toml": "[[agent\n",
        }.items():
            with self.subTest(name):
                broken.write_text(text)
                with mock.patch.object(check.load_rules, "__defaults__", (broken,)):
                    self.cannot_judge("cannot judge")

    def test_commit_missing_from_the_remote(self):
        event = {"pull_request": {"base": {"sha": self.repo.base}, "head": {"sha": "0" * 40}}}
        self.cannot_judge("git fetch failed", event=event, source="--remote")

    def test_remote_that_does_not_exist(self):
        self.repo.dir = str(self.root / "nowhere")
        event = {"pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}
        self.cannot_judge("git fetch failed", event=event, source="--remote")

    def test_git_that_cannot_be_started(self):
        event = {"pull_request": {"base": {"sha": self.repo.base}, "head": {"sha": self.repo.base}}}
        with mock.patch.object(check.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.cannot_judge("git rev-list failed", event=event)


class RulesFile(unittest.TestCase):
    def test_every_agent_has_a_name_and_at_least_one_rule(self):
        for a in RULES:
            with self.subTest(agent=a.name):
                self.assertTrue(a.name)
                self.assertTrue(a.emails or a.logins or a.name_suffixes or a.lines)

    def test_no_rule_is_a_bare_name(self):
        for a in RULES:
            for email in a.emails:
                self.assertIn("@", email, a.name)
            for pattern in a.lines:
                self.assertTrue(pattern.pattern.startswith("^"), pattern.pattern)

    def test_every_rule_is_the_one_this_table_lists(self):
        found = set()
        for a in RULES:
            found |= {(a.name, "email", e) for e in a.emails}
            found |= {(a.name, "login", n) for n in a.logins}
            found |= {(a.name, "suffix", s) for s in a.name_suffixes}
            found |= {(a.name, "line", p.pattern) for p in a.lines}
        self.assertEqual(found, EXPECTED_RULES)

    def test_every_listed_identity_is_caught(self):
        for name, kind, value in sorted(EXPECTED_RULES):
            with self.subTest(agent=name, kind=kind, value=value):
                if kind == "email":
                    self.assertEqual(check.agent_of_email(RULES, value), name)
                elif kind == "login":
                    self.assertEqual(check.agent_of_login(RULES, value), name)
                    noreply = f"1234+{value}@users.noreply.github.com"
                    self.assertEqual(check.agent_of_email(RULES, noreply), name)
                elif kind == "suffix":
                    self.assertEqual(check.agent_of_name(RULES, f"Ada{value}"), name)

    def test_every_listed_line_is_caught_by_its_example(self):
        for name, example in EXAMPLE_LINES:
            with self.subTest(example=example):
                found = check.check_text(RULES, "x", example)
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].endswith(f"({name})"), found)
        lines = {(a.name, p.pattern) for a in RULES for p in a.lines}
        covered = {
            (a.name, p.pattern)
            for a in RULES for p in a.lines for n, ex in EXAMPLE_LINES if n == a.name and p.search(ex)
        }
        self.assertEqual(lines, covered)

    def test_rules_are_compared_without_regard_to_case(self):
        path = self.enterContext(tempfile.TemporaryDirectory())
        rules_file = Path(path) / "rules.toml"
        rules_file.write_text(
            '[[agent]]\nname = "X"\nemails = ["Mixed@Example.ORG"]\nlogins = ["Some[BOT]"]\n'
        )
        rules = check.load_rules(rules_file)
        self.assertEqual(check.agent_of_email(rules, "mixed@example.org"), "X")
        self.assertEqual(check.agent_of_login(rules, "SOME[bot]"), "X")
        self.assertEqual(check.agent_of_email(rules, "1+some[bot]@users.noreply.github.com"), "X")

    def test_readme_gives_the_commands_the_check_prints(self):
        readme = (Path(check.__file__).resolve().parents[2] / "README.md").read_text()
        for command in ("git commit --amend --reset-author", "git push --force-with-lease"):
            self.assertIn(command, check.HOW_TO_FIX)
            self.assertIn(command, readme)

    def test_e_mail_is_stripped_before_it_is_compared(self):
        self.assertEqual(check.agent_of_email(RULES, " NoReply@Anthropic.com \t"), "Claude Code")


if __name__ == "__main__":
    unittest.main()
