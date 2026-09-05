# Feature workflow

One room identifies a feature in a project. The room keeps the specification, issues, backlog, decisions, Claude session identity, jobs, handoffs, and acceptance evidence needed to resume work later.

1. **Intent:** Astra identifies the user problem, examples, constraints, and scope. If the request is to build, that authorization is carried forward; if it is to plan, the workflow stops at the agreed spec.
2. **Specification:** Astra grounds requirements in the current repository and writes a self-contained revision with acceptance criteria and executable verification. Reference-based behavior gets a probe in the plan.
3. **Independent interpretation:** Fable states what it thinks the feature means before proposing improvements. Findings distinguish blockers from optional enhancements.
4. **Resolution:** Astra evaluates product implications and records each issue's disposition and rationale. Optional additions go to backlog. Fable owns routine engineering judgments. Meaningful unresolved product choices return to the user within the bounded review budget.
5. **Consensus:** Both agents accept the same revision and SHA-256, with blockers resolved. Changing the spec invalidates previous agreement.
6. **Handoff:** Astra binds existing implementation authorization and argument-array gates to the agreed spec. This record does not expand the user's scope or grant unrelated external actions.
7. **Implementation:** Fable chooses delegates under the fixed policy, supplies context, verifies returned code, runs gates and engineering reviews, and supplies evidence. A scope discovery creates a blocking issue; its resolution requires a strictly newer spec revision and renewed agreement before another handoff.
8. **Product acceptance:** Astra inspects delivered behavior and evidence independently. It records acceptance or specific findings. Known results can enter the diagnosed correction loop; uncertain delivery cannot.
9. **Delivery:** When the user requested the finished feature, Astra integrates the accepted work using normal repository tools, checks the pinned source baseline and current candidate evidence, preserves unrelated changes, and completes authorized review/publication steps. Integration that changes reviewed code requires renewed verification.

Astra need not read every delegate transcript. It inspects the code, behavior, engineering verdict, gate output, and remaining risks, then drills into routing or delegate details when a concern requires it. Fable remains accountable for every engineering verdict.

The shared room avoids using chat history as the only source of truth. Existing app conversations remain available; the plugin resumes dedicated worker sessions and explicit room records. The MCP server is a local tool surface, and the skill in the active Astra conversation drives the workflow. Jobs can outlive the connection, but the plugin does not create a future Astra wakeup automatically.
