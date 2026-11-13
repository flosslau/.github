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
into a temporary repository of its own, without their trees (a partial clone
with --filter=tree:0: the messages and the identities are in the commits);
no file of the pull request is checked out. The fetch brings the history the
two commits reach, commits only, whatever the size of the pull request; a
bound on that history waits for a run that is slow. --repo reads a clone
that already holds the commits.

A token for the fetch, when the repository needs one, comes in the
environment variable ATTRIBUTION_TOKEN. The check removes it from its own
environment before it starts any git, so no child inherits the variable
(the process's own initial environment and the shell above it still hold
it, readable by the same user on the runner), and hands it to the fetch
alone, as an Authorization header through GIT_CONFIG_* variables: never on
a command line, never in a file, and replaced by *** in a git error it
prints. Nothing else of the environment is withheld from git.

Every git call runs in a session of its own and ends within its timeout,
its helpers with it (git-remote-https holds the header too). Needs git 2.44
or newer: an older git ignores GIT_NO_LAZY_FETCH.

Exit status: 0 nothing found, 1 at least one finding, 2 the check could not
judge (not a pull request event, a commit that is missing or unreadable, a
fetch that failed three times, any error it did not expect).

Python 3.11 or newer, standard library only: the check runs on a bare hosted
runner, with no setup step and nothing to install.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

RULES_FILE = Path(__file__).resolve().parent / "rules.toml"
NOREPLY_HOST = "users.noreply.github.com"
MAX_FINDINGS_SHOWN = 50
TOKEN_VARIABLE = "ATTRIBUTION_TOKEN"

# Every git call ends: a hung one must not outlast the job. The fetch, the one
# call that talks to the network, gets more time and three attempts in all,
# with a pause between them; the sum stays inside the job's timeout-minutes
# (test_the_worst_case_of_the_fetch_fits_in_the_job_timeout pins it).
GIT_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 180
FETCH_BACKOFF_SECONDS = (5, 10)  # the pauses before the second and the third attempt
KILL_GRACE_SECONDS = 5  # after a timeout, how long the killed session gets to close its pipes
JOB_TIMEOUT_MINUTES = 10

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
    authors: tuple[Identity, ...]
    committers: tuple[Identity, ...]
    message: str


@dataclasses.dataclass(frozen=True)
class PullRequest:
    """What the check reads of the event, read once and checked on the way in."""

    base: str  # a commit id, 40 or 64 hex digits
    head: str
    login: str  # who opened it; empty when the event does not say
    title: str
    body: str


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


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of every git call: no prompt, and no lazy fetch.

    In a partial clone a git that meets a missing object fetches it from the
    promisor remote by itself, and that fetch can start another: measured on
    2026-11-12, against a remote that ignores the filter, it spawned fetches
    until the process limit. Nothing here needs an object the fetch left out.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", **(extra or {})}


def git(
    repo: str,
    *args: str,
    timeout: float = GIT_TIMEOUT_SECONDS,
    extra_env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    redact: tuple[str, ...] = (),
) -> bytes:
    """Run one git command and return its output; an error is "cannot judge".

    The command gets a session of its own, so that a timeout ends its helpers
    too (git-remote-https, index-pack), which would otherwise outlive it and
    keep the capture pipes, and the environment, open. The strings in
    `redact` are replaced by *** in an error before it is printed.
    """
    command = ["git", "--no-replace-objects", "-C", repo, *args]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment(extra_env),
            start_new_session=True,
        )
    except OSError as e:
        raise CannotJudge(f"git {args[0]} failed: {shown(str(e))}") from e
    try:
        out, err = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        end_session(process)
        raise CannotJudge(f"git {args[0]} did not end within {timeout:g} seconds") from e
    if process.returncode != 0:
        detail = err.decode(errors="replace")
        for secret in redact:
            if secret:
                detail = detail.replace(secret, "***")
        raise CannotJudge(f"git {args[0]} failed: {shown(detail.strip())}")
    return out


def end_session(process: subprocess.Popen) -> None:
    """Kill a git that ran out of time, with its session, and stop waiting on it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)  # the session: git and its helpers
    with contextlib.suppress(ProcessLookupError):
        process.kill()  # git itself, whatever became of the session
    try:
        process.communicate(timeout=KILL_GRACE_SECONDS)  # the pipes close once the session is dead
    except subprocess.TimeoutExpired:
        # Something outlived the session and holds the pipes: its output is
        # not worth waiting for, and the check must still end.
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=KILL_GRACE_SECONDS)


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


def parse_commit(sha: str, raw: bytes) -> Commit:
    """The commit as git stores it, so that no .mailmap rewrites who wrote it.

    Every author and every committer line is kept: readers disagree on which
    one counts when a commit holds two, so each is judged. One that cannot be
    read makes the commit unjudgeable.
    """
    head, _, message = raw.partition(b"\n\n")
    who: dict[bytes, list[Identity]] = {b"author": [], b"committer": []}
    for line in head.split(b"\n"):
        role, _, value = line.partition(b" ")
        if role in who:
            identity = read_identity(value)
            if identity is None:
                raise CannotJudge(f"commit {sha[:7]}: author or committer not readable")
            who[role].append(identity)
    if not who[b"author"] or not who[b"committer"]:
        raise CannotJudge(f"commit {sha[:7]}: author or committer not readable")
    return Commit(
        sha, tuple(who[b"author"]), tuple(who[b"committer"]), message.decode(errors="replace")
    )


def commits_of(repo: str, base: str, head: str) -> list[Commit]:
    """The commits the pull request adds, oldest first, read in one git process."""
    shas = git(repo, "rev-list", "--reverse", f"{base}..{head}").decode().split()
    if not shas:
        return []
    batch = git(repo, "cat-file", "--batch", stdin="".join(f"{sha}\n" for sha in shas).encode())
    commits, offset = [], 0
    for sha in shas:
        end = batch.find(b"\n", offset)
        fields = batch[offset:end].split() if end >= 0 else []  # "<sha> commit <size>", or "<sha> missing"
        if len(fields) != 3 or fields[0] != sha.encode() or fields[1] != b"commit":
            raise CannotJudge(f"commit {sha[:7]}: not a commit the repository holds")
        size = int(fields[2])
        raw = batch[end + 1 : end + 1 + size]
        if len(raw) != size:
            raise CannotJudge(f"commit {sha[:7]}: cut short in git's answer")
        offset = end + 1 + size + 1  # the newline git writes after each object
        commits.append(parse_commit(sha, raw))
    return commits


def authorization_header(token: str) -> str:
    return "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def fetch_environment(remote: str, token: str) -> dict[str, str]:
    """The token, for this fetch only, as the header actions/checkout sends.

    GIT_CONFIG_* puts it in git's configuration for one process: not on the
    command line, which `ps` shows, and not in a file of the repository.
    """
    if not token:
        return {}
    header = authorization_header(token)
    # Appended after any entry the environment already holds, which stays.
    try:
        count = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        count = 0
    return {
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": f"http.{remote}.extraheader",
        f"GIT_CONFIG_VALUE_{count}": header,
    }


def fetch(remote: str, into: str, base: str, head: str, token: str = "") -> None:
    """Fetch the two commits, and their history without trees, into an empty repository.

    The remote gets a name: git refuses a bare path as the promisor of a
    filtered fetch. Three attempts in all, with the pauses of
    FETCH_BACKOFF_SECONDS between them: every failure is retried, a timeout
    or a refusal alike, since telling a passing one from a lasting one is not
    worth the code, and the cost of retrying a lasting one is two pauses.
    """
    git(into, "init", "--quiet")
    git(into, "remote", "add", "origin", remote)
    extra = fetch_environment(remote, token)
    attempts = len(FETCH_BACKOFF_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            git(
                into, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
                "--filter=tree:0", "origin", base, head,
                timeout=FETCH_TIMEOUT_SECONDS, extra_env=extra,
                redact=(token, authorization_header(token)),
            )
            return
        except CannotJudge as e:
            if attempt == attempts:
                raise CannotJudge(f"after {attempt} attempts, {e}") from e
            time.sleep(FETCH_BACKOFF_SECONDS[attempt - 1])


def read_pull_request(event: object) -> PullRequest:
    """The pull request of a pull_request event; anything else cannot be judged."""
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict):
        raise CannotJudge("the event is not a pull request event")
    user = pr.get("user") or {}
    if not isinstance(user, dict):
        raise CannotJudge("the event's user is not an object")
    return PullRequest(
        base=commit_id(pr, "base"),
        head=commit_id(pr, "head"),
        login=event_text(user.get("login"), "user login"),
        title=event_text(pr.get("title"), "title"),
        body=event_text(pr.get("body"), "description"),
    )


def commit_id(pr: dict, side: str) -> str:
    try:
        sha = pr[side]["sha"]
    except (KeyError, TypeError) as e:
        raise CannotJudge(f"the event lacks {e}") from e
    if not (isinstance(sha, str) and SHA.fullmatch(sha)):
        raise CannotJudge(f"not a commit id: {shown(str(sha))}")
    return sha


def event_text(value: object, what: str) -> str:
    """A text of the event; absent or null reads as empty."""
    value = value or ""
    if not isinstance(value, str):
        raise CannotJudge(f"the event's {what} is not text")
    return value


def check_pull_request(
    rules: list[Agent], event: object, repo: str | None, remote: str | None, token: str = ""
) -> list[str]:
    pr = read_pull_request(event)
    found = []
    if agent := agent_of_login(rules, pr.login):
        found.append(f"pull request, opened by {shown(pr.login)} ({agent})")
    found += check_text(rules, "pull request title", pr.title)
    found += check_text(rules, "pull request description", pr.body)
    # A scratch repository git could not fully remove is left behind, not a
    # reason to fail: the runner is thrown away with it.
    with tempfile.TemporaryDirectory(prefix="attribution-", ignore_cleanup_errors=True) as scratch:
        if remote is not None:
            fetch(remote, scratch, pr.base, pr.head, token)
            repo = scratch
        for c in commits_of(repo, pr.base, pr.head):
            where = f"commit {c.sha[:7]}"
            for who in c.authors:
                found += check_identity(rules, f"{where}, author", who)
            for who in c.committers:
                found += check_identity(rules, f"{where}, committer", who)
            found += check_text(rules, f"{where}, message", c.message)
    return found


def main(argv: list[str] | None = None) -> int:
    # Out of the environment before anything else runs: no child inherits the
    # variable (this process's initial environment block still holds it).
    token = os.environ.pop(TOKEN_VARIABLE, "")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True, help="pull_request event JSON file")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--remote", help="repository to fetch the commits from")
    source.add_argument("--repo", help="clone that already holds the commits")
    args = parser.parse_args(argv)
    try:
        with open(args.event, encoding="utf-8") as f:
            event = json.load(f)
        found = check_pull_request(load_rules(), event, args.repo, args.remote, token)
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
