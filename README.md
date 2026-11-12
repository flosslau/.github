<!--
Copyright 2026 Bruno Venceslau
SPDX-License-Identifier: Apache-2.0
-->

# flosslau/.github

What the repositories of this organization share.

## Attribution check

A commit in these repositories is written by a person, who answers for it. The
check refuses a pull request that carries the identity or the attribution line
of a coding agent:

- an author or a committer that is an agent, by e-mail address or bot account;
- a trailer that names one, such as a co-author line with an agent's address;
- a fixed line an agent adds to a commit message, or to the title or the
  description of a pull request, such as a "Generated with" footer or a session
  link;
- a pull request opened by an agent's bot account.

It does not refuse a name by itself, a message that mentions a tool, or the
bots that update dependencies. [`rules.toml`](actions/attribution/rules.toml)
is the list, with the evidence behind each entry.

It reads every commit the pull request adds, whoever made it: a pull request
that carries someone else's commits, such as a backport, is judged by them too.

Using an agent to write code is not what the check is about: it looks at who
signs the commit and at the lines listed above, nothing else.

### Fixing a refused pull request

The failed job prints each finding: the commit, the field or the line, and the
agent it belongs to.

- A commit: make yourself its author and remove the line, then push again. For
  the last commit:

  ```
  git commit --amend --reset-author
  git push --force-with-lease
  ```

  For older commits, `git rebase -i` and amend each one the same way.
- The title or the description: edit the pull request. The check runs again.

To quote a refused line, in a commit message or a description, indent it,
put it in a list or a quote, or use inline code: the check reads a line only
from its first column. One word that is not ASCII in front of the "Generated
with" footer, an emoji or a bullet such as U+2022, does not make a quote: the
footer comes with an emoji in front, and is refused with it.

### Using it in a repository

Add a workflow of its own, so that editing a title does not run the other
checks again:

```yaml
name: Attribution

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read

concurrency:
  group: attribution-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  attribution:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: flosslau/.github/actions/attribution@<commit SHA>
```

- Pin the action to a commit of this repository's `main`. Nothing else needs a
  pin: the action uses no other action.
- Keep the `permissions` block: `contents: read` is all the check needs. The
  job's own token is passed to the fetch (the action's input `token`, which
  defaults to it), as an Authorization header for the repository's host and
  for nothing else; no secret is needed, on a public or a private
  repository. On a pull request from a fork that token can only read, which
  is all the check does. Whatever token a caller passes instead goes to that
  same host.
- Keep `timeout-minutes: 10`. The fetch has three attempts of 180 seconds
  with short pauses between them, and every other git call ends within 30
  seconds; the job's timeout is the backstop.
- Use `pull_request`, as above, and not `pull_request_target`.
- The job needs no checkout step. The action fetches the commits of the pull
  request, without their trees, into a temporary repository and checks out
  none of its files. The fetch brings the commit history the two commits
  reach, whatever the size of the pull request; a bound on it waits for a
  run that is slow.
- The runner needs git 2.44 or newer (`ubuntu-latest` has it): an older git
  ignores the setting that keeps the fetch from asking for missing objects.
- To make the check binding, require the `attribution` status check on the
  default branch.

What it does not do:

- It reads pull requests only. A commit pushed straight to a branch is not
  judged, which a rule that requires pull requests on the default branch
  covers.
- A run started again by hand reads the title and the description of the
  first time. Edit the pull request, or push, to have the current ones judged.
- In this repository itself a pull request can change the check that judges
  it, so here the result does not replace a review.

### Working on it

`make check` runs the tests, with Python 3.11 or newer and git; there is
nothing to install. Every commit the tests judge is made in a throwaway
repository.

A new entry in `rules.toml` needs evidence, written next to it: a string
measured in real commits, an account that exists, or a quote from the tool's
own source.

## License

Apache License 2.0. See [LICENSE](LICENSE).
