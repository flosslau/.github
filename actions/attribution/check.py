#!/usr/bin/env python3
# Copyright 2026 Bruno Venceslau
# SPDX-License-Identifier: Apache-2.0
"""Refuse the identity and the attribution lines of coding agents.

Reads one pull request: the author and the committer of each of its commits,
each line of their messages, the title and the description of the pull
request, and the login of who opened it. rules.toml, next to this file, lists
what is refused.

Usage:
    check.py --event <pull_request event JSON> --remote <repository URL>
    check.py --event <pull_request event JSON> --repo <clone with the commits>

With --remote the check fetches the base and the head commits of the event
into a temporary repository of its own; no file of the pull request is
checked out. --repo reads a clone that already holds them.

Exit status: 0 nothing found, 1 at least one finding, 2 the check could not
judge (not a pull request event, a commit that is missing or unreadable, any
error it did not expect).

Python 3.11 or newer, standard library only: the check runs on a bare hosted
runner, with no setup step and nothing to install.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

RULES_FILE = Path(__file__).resolve().parent / "rules.toml"
NOREPLY_HOST = "users.noreply.github.com"
MAX_FINDINGS_SHOWN = 50

# A trailer whose value ends with an address: "Co-authored-by: Name <a@b>".
# The key starts the line, so an indented or quoted example does not match.
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[ \t].*<([^<>\s]+)>[ \t]*$")
# "Name <address> 1700000000 +0000", the value of an author or committer line.
IDENTITY = re.compile(rb"^(.*) \d+ [+-]\d{4}$")
ADDRESS = re.compile(rb"<([^<>]*)>")
SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

HOW_TO_FIX = """\
How to fix it:
  - A commit: make yourself its author and remove the line, then push again.
    For the last commit:
        git commit --amend --reset-author
        git push --force-with-lease
    For older commits, `git rebase -i` and amend each one the same way.
  - The title or the description: edit the pull request. The check runs again.
The rule and its list: https://github.com/flosslau/.github/tree/main/actions/attribution"""


class CannotJudge(Exception):
    """The input does not let the check reach a verdict."""


@dataclasses.dataclass(frozen=True)
class Agent:
    name: str
    emails: frozenset[str]
    logins: frozenset[str]
    name_suffixes: tuple[str, ...]
    lines: tuple[re.Pattern[str], ...]


@dataclasses.dataclass(frozen=True)
class Identity:
    name: str
    emails: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    author: Identity
    committer: Identity
    message: str


def load_rules(path: Path = RULES_FILE) -> list[Agent]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return [
        Agent(
            name=a["name"],
            emails=frozenset(e.casefold() for e in a.get("emails", [])),
            logins=frozenset(n.casefold() for n in a.get("logins", [])),
            name_suffixes=tuple(a.get("name_suffixes", [])),
            lines=tuple(re.compile(p) for p in a.get("lines", [])),
        )
        for a in data["agent"]
    ]


def agent_of_login(rules: list[Agent], login: str) -> str | None:
    login = login.casefold()
    return next((a.name for a in rules if login in a.logins), None)


def agent_of_email(rules: list[Agent], email: str) -> str | None:
    email = email.strip().casefold()
    for a in rules:
        if email in a.emails:
            return a.name
    local, _, host = email.rpartition("@")
    if host != NOREPLY_HOST:
        return None
    # GitHub writes <id>+<login>@... and, on older commits, <login>@...
    return agent_of_login(rules, local.partition("+")[2] or local)


def agent_of_name(rules: list[Agent], name: str) -> str | None:
    return next(
        (a.name for a in rules if any(name.endswith(s) for s in a.name_suffixes)),
        None,
    )


def shown(text: str) -> str:
    """Untrusted text, made safe to print in a workflow log."""
    text = "".join(c if c.isprintable() else "?" for c in text)
    return text.replace("::", ": :")[:200]


def check_identity(rules: list[Agent], where: str, who: Identity) -> list[str]:
    found = []
    for email in who.emails:
        if agent := agent_of_email(rules, email):
            found.append(f"{where}: e-mail {shown(email)} ({agent})")
    if agent := agent_of_name(rules, who.name):
        found.append(f"{where}: name {shown(who.name)} ({agent})")
    return found


def check_text(rules: list[Agent], where: str, text: str) -> list[str]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        agents = []
        if m := TRAILER.match(line):
            agents.append(agent_of_email(rules, m.group(1)))
        agents += [a.name for a in rules if any(p.search(line) for p in a.lines)]
        if agent := next((a for a in agents if a), None):
            found.append(f"{where}, line {number}: {shown(line)} ({agent})")
    return found


def git(repo: str, *args: str) -> bytes:
    command = ["git", "--no-replace-objects", "-C", repo, *args]
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or str(e).encode()
        raise CannotJudge(
            f"git {args[0]} failed: {shown(detail.decode(errors='replace').strip())}"
        ) from e


def read_identity(value: bytes) -> Identity | None:
    """Every address of an author or committer line, however a reader splits it."""
    m = IDENTITY.match(value)
    emails = ADDRESS.findall(m.group(1)) if m else []
    if not emails:
        return None
    name = m.group(1).partition(b"<")[0].strip()
    return Identity(
        name.decode(errors="replace"),
        tuple(e.decode(errors="replace") for e in emails),
    )


def read_commit(repo: str, sha: str) -> Commit:
    """The commit as git stores it, so that no .mailmap rewrites who wrote it."""
    raw = git(repo, "cat-file", "commit", sha)
    head, _, message = raw.partition(b"\n\n")
    who = {}
    for line in head.split(b"\n"):
        role, _, value = line.partition(b" ")
        if role in (b"author", b"committer") and (identity := read_identity(value)):
            who[role] = identity
    if len(who) != 2:
        raise CannotJudge(f"commit {sha[:7]}: author or committer not readable")
    return Commit(sha, who[b"author"], who[b"committer"], message.decode(errors="replace"))


def commits_of(repo: str, base: str, head: str) -> list[Commit]:
    shas = git(repo, "rev-list", "--reverse", f"{base}..{head}").decode().split()
    return [read_commit(repo, sha) for sha in shas]


def fetch(remote: str, into: str, base: str, head: str) -> None:
    """Fetch the two commits, and their history, into an empty repository."""
    git(into, "init", "--quiet")
    git(into, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", remote, base, head)


def check_pull_request(
    rules: list[Agent], event: object, repo: str | None, remote: str | None
) -> list[str]:
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict):
        raise CannotJudge("the event is not a pull request event")
    try:
        base, head = pr["base"]["sha"], pr["head"]["sha"]
    except (KeyError, TypeError) as e:
        raise CannotJudge(f"the event lacks {e}") from e
    for sha in (base, head):
        if not (isinstance(sha, str) and SHA.fullmatch(sha)):
            raise CannotJudge(f"not a commit id: {shown(str(sha))}")
    login = (pr.get("user") or {}).get("login") or ""
    title, body = pr.get("title") or "", pr.get("body") or ""

    found = []
    if agent := agent_of_login(rules, login):
        found.append(f"pull request, opened by {shown(login)} ({agent})")
    found += check_text(rules, "pull request title", title)
    found += check_text(rules, "pull request description", body)
    with tempfile.TemporaryDirectory(prefix="attribution-") as scratch:
        if remote is not None:
            fetch(remote, scratch, base, head)
            repo = scratch
        for c in commits_of(repo, base, head):
            where = f"commit {c.sha[:7]}"
            found += check_identity(rules, f"{where}, author", c.author)
            found += check_identity(rules, f"{where}, committer", c.committer)
            found += check_text(rules, f"{where}, message", c.message)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True, help="pull_request event JSON file")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--remote", help="repository to fetch the commits from")
    source.add_argument("--repo", help="clone that already holds the commits")
    args = parser.parse_args(argv)
    try:
        with open(args.event, encoding="utf-8") as f:
            event = json.load(f)
        found = check_pull_request(load_rules(), event, args.repo, args.remote)
    except Exception as e:  # anything unexpected is "cannot judge", never a finding
        why = str(e) if isinstance(e, CannotJudge) else f"{type(e).__name__}: {e}"
        print(f"attribution check: cannot judge: {shown(why)}")
        return 2
    if not found:
        print("attribution check: no agent identity or attribution line found")
        return 0
    print(f"attribution check: {len(found)} finding(s)")
    for line in found[:MAX_FINDINGS_SHOWN]:
        print(f"  {line}")
    if len(found) > MAX_FINDINGS_SHOWN:
        print(f"  ... and {len(found) - MAX_FINDINGS_SHOWN} more")
    print(HOW_TO_FIX)
    return 1


if __name__ == "__main__":
    sys.exit(main())
