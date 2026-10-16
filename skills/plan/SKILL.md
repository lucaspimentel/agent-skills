---
name: plan
description: Produce a self-contained implementation plan as a handoff prompt. Invoke explicitly with /plan <task>, or /plan with no task to infer one from the conversation.
argument-hint: "[task]"
disable-model-invocation: true
---

Plan the task without changing anything, then present the plan as a handoff prompt and stop.

## Planning discipline

Research with read-only tools only: read, grep, glob, and bash commands that only gather context. Do not modify files, do not call write or edit tools, and do not run git mutations until the user approves the plan. The rule binds every agent in the chain: you, and any subagent you dispatch.

Resolve genuine design ambiguity with the user before writing the plan; anything discoverable by reading the code is yours to find, not theirs.

If invoked with no task argument, infer the task from the conversation so far: what the user has been discussing, asking about, or working on. If the intent is ambiguous, ask. If nothing can be inferred, reply with a single sentence asking what to plan.

## Delegation

When the host provides a subagent delegation tool, delegate the whole planning job to a subagent instead of planning inline. Try dispatch targets in this order, skipping any that a prior dispatch error already ruled out; fall back to inline planning under Planning discipline only when no rung applies.

1. A planner agent: one whose role is producing implementation plans (in pi's subagent extension: `planner`).
2. A read-only agent: an unknown-agent error lists the available agents; pick one whose toolset is restricted to read-style tools (in pi's subagent extension: `scout`).
3. Any remaining listed agent, even one with write tools: the dispatch task's no-modification rule is then the only guard, so state it prominently.
4. No delegation tool: plan inline yourself.

### Dispatch task

Subagents run in isolated context, so the dispatch must be self-contained. Include:

- The task: verbatim when /plan was called with one, otherwise the inferred task from Planning discipline.
- The read-only rule: research only, no file modifications, no git mutations.
- The full output contract from the section below, restated: imperative voice, self-contained, numbered steps with verification criteria, raw Markdown, first line is the plan's first line.
- An instruction to do its own research with its read tools before writing the plan.

### Presenting the result

Relay the subagent's plan verbatim: it is the handoff prompt, not raw material to rewrite. Check it against the output contract before presenting, and fix missing required elements inline if needed (for example, no verification criteria). The stop-and-wait in the last section is always yours: present the plan yourself and never delegate waiting for approval.

## Output: the handoff prompt

Write the plan as a complete, self-contained handoff prompt that a fresh agent session (with no memory of this conversation) could be given directly and implement from scratch.

- Written in imperative voice, addressed to the implementing agent.
- Self-contained: include the task, relevant file paths discovered during research, constraints, a numbered step-by-step implementation plan, and concrete verification criteria. Never refer to "the discussion above" or to anything outside the plan.
- The plan is your entire reply and nothing else: no meta commentary, no "here is the plan" preamble.
- Output it as raw Markdown with no surrounding code fence; the first line of the reply is the plan's own first heading or line.

## After the plan

Stop and wait. Do not implement. If the user replies with feedback, produce a revised plan under the same requirements before anything else. Implementation starts only when the user says go.
