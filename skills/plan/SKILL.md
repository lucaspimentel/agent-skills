---
name: plan
description: Produce a self-contained implementation plan as a handoff prompt. Invoke explicitly with /plan <task>, or /plan with no task to infer one from the conversation.
argument-hint: "<task>"
disable-model-invocation: true
---

Plan the task without changing anything, then present the plan as a handoff prompt and stop.

## Planning discipline

Research with read-only tools only: read, grep, glob, and bash commands that only gather context. Do not modify files, do not call write or edit tools, and do not run git mutations until the user approves the plan.

Resolve genuine design ambiguity with the user before writing the plan; anything discoverable by reading the code is yours to find, not theirs.

If invoked with no task argument, infer the task from the conversation so far: what the user has been discussing, asking about, or working on. If the intent is ambiguous, ask. If nothing can be inferred, reply with a single sentence asking what to plan.

## Output: the handoff prompt

Write the plan as a complete, self-contained handoff prompt that a fresh agent session (with no memory of this conversation) could be given directly and implement from scratch.

- Written in imperative voice, addressed to the implementing agent.
- Self-contained: include the task, relevant file paths discovered during research, constraints, a numbered step-by-step implementation plan, and concrete verification criteria. Never refer to "the discussion above" or to anything outside the plan.
- The plan is your entire reply and nothing else: no meta commentary, no "here is the plan" preamble.
- Output it as raw Markdown with no surrounding code fence; the first line of the reply is the plan's own first heading or line.

## After the plan

Stop and wait. Do not implement. If the user replies with feedback, produce a revised plan under the same requirements before anything else. Implementation starts only when the user says go.
