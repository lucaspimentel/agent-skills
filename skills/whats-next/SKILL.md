---
name: whats-next
description: "Show a prioritized list of incomplete tasks from TODO.md. Use when the user says 'what's next', 'next task', 'what should I work on', 'what's up next', 'what tasks remain', or any variation of wanting to see the outstanding TODO items."
disable-model-invocation: true
---

Read TODO.md to find incomplete tasks (unchecked checkboxes, items not marked done, etc.). If TODO.md doesn't exist, inform the user and suggest creating one with the add-todo skill.

List the incomplete tasks ordered by best "bang for the buck" — prioritize items that are high-impact and easy to implement (low-hanging fruit) over items that are low-impact or complex. Use task labels, size estimates, or dependency information if available in the file. If no such metadata is present, present tasks in file order.

Output a numbered list, then stop. Do not ask which item to work on, do not use AskUserQuestion, and do not begin working on any task. Wait for the user's next message.
