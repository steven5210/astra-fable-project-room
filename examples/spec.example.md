# Example feature: task-list filtering

Revision: 1

## Problem and intended outcome

A person reviewing a local task list needs to focus on unfinished work without losing completed items. Add a status filter to the existing list view.

## Desired behavior

- Provide three choices: All, Active, and Completed. All is selected on a fresh page load.
- Active shows tasks whose existing `completed` value is false. Completed shows tasks whose value is true. All shows both.
- Filtering preserves the existing task order, identifiers, and task data. Switching filters never changes task completion state.
- After a task's completion state changes, the current filtered list updates immediately.
- An empty filtered list displays a message naming that filter, such as “No active tasks.”
- The selected filter is visually distinguishable and available to keyboard and screen-reader users.

## Scope and constraints

This example is a specification review only. Persistence of the selected filter, sorting, search, task deletion, and server-side changes are outside scope. No repository or actual task-list implementation is attached; identify implementation-specific assumptions instead of inventing interfaces.

## Acceptance criteria

1. Given one active task and one completed task, each filter displays exactly its matching tasks, with original relative order preserved.
2. Given Active is selected, completing its last task removes that item from the visible list and displays the active-filter empty message.
3. Changing filters does not modify the underlying task objects.
4. A reviewer can identify the selected filter and change it without a pointer.
5. The review states any remaining ambiguity in behavior or verification before accepting the revision.

## Requested review

State your independent interpretation, then identify blocking ambiguities and optional suggestions separately. Return the room's structured review schema and cite the exact revision and digest from its packet. Do not implement this feature.
