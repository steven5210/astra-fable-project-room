# Feature workflow

One room identifies a feature in a project. The room keeps the specification, issues, backlog, decisions, Claude session identity, jobs, handoffs, and acceptance evidence needed to resume work later.

1. **Intent:** Astra identifies the user problem, examples, constraints, and scope. If the request is to build, that authorization is carried forward; if it is to plan, the workflow stops at the agreed spec.
2. **Specification:** Astra grounds requirements in the current repository and writes a self-contained revision with acceptance criteria and executable verification. Reference-based behavior gets a probe in the plan.
3. **Independent interpretation:** Fable states what it thinks the feature means and proposes useful improvements with their benefits and tradeoffs. Findings distinguish blockers from optional enhancements.
4. **Resolution:** Astra evaluates product implications and records each finding's disposition and rationale. Astra surfaces enhancement proposals, files or links project GitHub issues under existing filing authorization, and asks the user for their opinion and scope approval. The room retains the proposal, issue link, and decision; unavailable filing stays explicitly pending. Fable owns routine engineering judgments. After three reviews, further debate returns a focused product decision to the user. Recording the actual answer permits the next bounded round and retains all earlier attempts; existing agreement proceeds to handoff.
5. **Consensus:** Both agents accept the same revision and SHA-256, with blockers resolved. Changing the spec invalidates previous agreement.
6. **Handoff:** Astra binds existing implementation authorization and argument-array gates to the agreed spec. This record does not expand the user's scope or grant unrelated external actions.
7. **Implementation:** Fable chooses delegates under the fixed policy, supplies context, verifies returned code, runs gates and engineering reviews, and supplies evidence. A scope discovery creates a blocking issue; its resolution requires a strictly newer spec revision and renewed agreement before another handoff.
8. **Product acceptance:** Astra inspects delivered behavior and evidence independently. It records acceptance or specific findings. Known results can enter the diagnosed correction loop; uncertain delivery cannot.
9. **Delivery:** When the user requested the finished feature, Astra integrates the accepted work using normal repository tools, checks the pinned source baseline and current candidate evidence, preserves unrelated changes, and completes authorized review/publication steps. Integration that changes reviewed code requires renewed verification.

Astra need not read every delegate transcript. It inspects the code, behavior, engineering verdict, gate output, and remaining risks, then drills into routing or delegate details when a concern requires it. Fable remains accountable for every engineering verdict.

Filing an enhancement issue is separate from approving its implementation. Astra checks for an existing issue before creating one, shows the user its verified link, and continues agreed work while awaiting the user's answer. Approved scope is recorded in a revised specification and reviewed before implementation; optional ideas must not disappear silently into a backlog.

The shared room avoids using chat history as the only source of truth. Existing app conversations remain available; the plugin resumes dedicated worker sessions and explicit room records. The MCP server is a local tool surface, and the skill in the active Astra conversation drives the workflow. Jobs can outlive the connection, but the plugin does not create a future Astra wakeup automatically.
