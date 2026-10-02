# atlassian

Working with Jira, Confluence, and other Atlassian products via the Atlassian CLI (`acli`) or the hosted Atlassian MCP server: authentication, JQL/CQL searches, bulk operations, sprint reports, cross-product queries.

Authored by [Jakob He](https://github.com/leweii), repackaged from [leweii/atlassian-cli](https://github.com/leweii/atlassian-cli) under the MIT License. The skill has been locally updated beyond upstream v1.0.0 with command-surface additions (the `board get` deprecation note, additional `workitem` actions, and Confluence `page`/`blog` entities); re-check upstream before treating any copy differences as unintentional drift.

See [installation instructions](../../README.md#install).

## Prerequisites

Requires the [Atlassian CLI (`acli`)](https://developer.atlassian.com/cloud/cli/) to be installed and on `PATH`. Verify with `acli --version`.

## Skills

| Skill | Description |
|---|---|
| `atlassian` | Authentication, modern command syntax, batch operations with JQL, bulk creation, and output formats for `acli`, plus when to use the Atlassian MCP server instead |
