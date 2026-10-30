# TODO

## address-pr-comments: read review threads through `gh_read`

`skills/address-pr-comments/SKILL.md` shells out to `gh pr-threads list` and
depends on that gh extension being installed (`SKILL.md:10`). pi's `gh_read`
now provides the same read as its `pr threads` operation, with resolved and
outdated state and bounded cursor paging, so the listing step should use it and
the extension should be needed only for the write commands.

- [ ] Replace the listing invocation (`SKILL.md:23`) with `gh_read`
      `{ subcommand: "pr threads", repo: "OWNER/REPO", pr: <number> }`, and
      re-scope or drop the install requirement at `SKILL.md:10`.
- [ ] Rewrite the output-shape paragraph (`SKILL.md:26`): the result is the
      verbatim `reviewThreads.nodes` array, not `{ prUrl, unresolvedCount,
      threads }`. Metadata lines carry `pr_url`, `threads_total`,
      `threads_returned`, `source_complete`, `pages_fetched`, and
      `threads_with_truncated_comments`; a `continuation` token appears when
      more pages exist.
- [ ] Field rewrite: `author` becomes `comments.nodes[].author.login`;
      `authorIsBot` becomes `comments.nodes[].author.__typename == "Bot"`;
      `comments[]` becomes `comments.nodes[]`; `databaseId` stays numeric and
      still feeds `--in-reply-to`.
- [ ] Resolved filtering: `pr threads` returns resolved and outdated threads
      unfiltered, unlike `list` which filtered by default and offered `--all`.
      Filter locally, for example `jq '[.[] | select(.isResolved == false)]'`,
      and update the prose at `SKILL.md:28`.
- [ ] Update the bot-prefix and trust-warning prose that reads `authorIsBot`
      (`SKILL.md:30`, `:42`, `:48`).
- [ ] State the per-thread comment limit: each thread carries at most its first
      100 comments and deeper per-thread paging is unsupported, so a thread
      with more comments must be reported as truncated rather than silently
      treated as complete.
- [ ] `THREAD_ID` stays the thread's `PRRT_...` node id and `COMMENT_ID` stays
      the numeric `databaseId` (`SKILL.md:147`, `:156`): both still feed the
      write commands, which are unchanged.

Tracking: pi-extensions `TODO.md` (external follow-up).
