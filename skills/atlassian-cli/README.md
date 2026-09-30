# atlassian-cli v1.0.0

Atlassian CLI (`acli`) usage for Jira and Confluence: authentication, JQL searches, bulk operations, sprint reports.

Authored by [Jakob He](https://github.com/leweii), repackaged from [leweii/atlassian-cli](https://github.com/leweii/atlassian-cli) under the MIT License. The skill has been locally updated beyond upstream v1.0.0 with command-surface additions (the `board get` deprecation note, additional `workitem` actions, and Confluence `page`/`blog` entities); re-check upstream before treating any copy differences as unintentional drift.

See [installation instructions](../../README.md#installation).

## Prerequisites

Requires the [Atlassian CLI (`acli`)](https://developer.atlassian.com/cloud/cli/) to be installed and on `PATH`. Verify with `acli --version`.

## Skills

| Skill | Description |
|---|---|
| `atlassian-cli` | Authentication, modern command syntax, batch operations with JQL, bulk creation, and output formats for `acli` |
