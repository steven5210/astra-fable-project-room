"""Compact model judgment, with explicitly controller-owned immutable metadata."""
from room import RoomError

FORMAT = 'project_room_engineering_v2'
PART = 'efficiency_contract_v1'
DELEGATION_PART = 'delegation_efficiency_v2'
DELEGATION_INSTRUCTION = '''Delegation operating amendment (new work only; supersedes typing-only supporting-worker defaults):
Keep Fable and native workers at MAX and retain all pinned provider budgets and quality gates. During authorized implementation, give qualified delegates complete requirements, verified interfaces and acceptance checks so they author supporting code, scripts and tests. Do not first write the complete implementation yourself merely to have Sonnet type it. Sonnet may make routine local implementation choices within the agreed contract; escalate missing requirements, conflicting evidence and cross-module judgment to Fable. An actual exact-copy requirement still applies. Fable owns engineering specifications, necessary source/evidence review, adjudication and the final verdict; record a concrete quality reason when retaining supporting authorship. DeepSeek remains the first substantive choice when eligible at full quality; preserve actual destination-specific data restrictions. Exact-spec review remains read-only and non-delegating.
Bound work by complete functional units and verification requirements. Remove operator-imposed hard source-line and Write-call counts used only to reduce output. Do not compress or repeatedly rewrite correct readable code to meet those counts. Preserve substantive requirements and explicit product/format constraints.
Keep routine inventories and gate collection with the assigned operator when no model judgment is needed. If delegated, retain complete metadata and logs in an authorized artifact outside the candidate worktree or in an already ignored path. Do not change ignore rules or add candidate files merely to store operational evidence; explicitly requested deliverables retain their authorized product paths. Return concise counts, relative paths, digests, material findings and unresolved gaps. Do not request raw recursive listings, repeated full paths or verbatim logs by default. Read-only tasks do not gain write permission: use existing evidence or request operator collection. The receiving reviewer must actually read and verify any complete evidence needed for its verdict; summaries and file references alone do not certify quality.
An explicit account/session quota failure is an operational blocker, not a capability miss. Stop new model/delegate submissions in that failed native turn; do not try Opus after Sonnet or another route merely to evade the same quota. Preserve the failure and report the blocker for the existing audited continuation process. Generic transient or model-specific rate limits require their own diagnosis. No automatic retry, quota reset, lower effort, provider switch, review-budget renewal or resume authorization is granted by this amendment.
This amendment changes task packaging and failure handling only. Keep the existing report format and record actual routing outcomes. Deliver it once, without repeating the settled specification, earlier policies or historic findings on subsequent continuations.'''
INSTRUCTION = '''Operating/reporting update (supersedes earlier reporting and authorship defaults):
Fable remains at MAX and owns engineering judgment, routing, independent evidence review and the final verdict. A request that Fable personally review a subject does not require Fable to author all supporting documents or forbid supporting delegation. Preserve any explicit user prohibition or personal-authorship requirement narrowly; never add one by inference. DeepSeek remains the first substantive choice when it meets full quality; Fable selects Sonnet, Opus or itself when needed. During authorized implementation, delegate evidence indexing, document assembly, implementation, tests and routine work; the operator can persist and validate complete outputs. Exact-spec review still has no delegation. Personally inspect the evidence needed for your verdict without reproducing settled delegate work. If you retain supporting authorship or routine analysis, record the concrete quality/capability reason in routing_log. Quality never decreases to save tokens.
Retire repeated historical provider-attempt tables, historical tool counts and duplicated status from follow-up reports. The operator maintains those ledgers. Report only current findings, decisions, actual routing, unresolved gaps and relevant evidence. Reuse settled findings; reopen a decision only for new evidence or an identified gap. If an artifact is requested, put one complete bounded artifact before optional reporting and identify unfinished units honestly. Do not claim an artifact was delivered until its complete content or verified saved bytes exist. Delegate long document assembly; length targets are not reasons to repeat findings.
For implementation/correction replies, use this compact JSON contract instead of copying controller identifiers:
{"report_format":"project_room_engineering_v2","outcome":"changes_required","implementation_complete":false,"changes":[],"tests_reported":[],"review_findings":[],"remaining_gaps":[],"backlog":[],"routing_log":[]}
This illustrates field types, not your verdict. outcome must be completed, changes_required or scope_change. Only completed/true with no remaining_gaps is eligible for independent acceptance. Each routing_log entry retains delegate_job_ids as a list naming only relevant room-owned jobs. Fill the judgment fields truthfully; do not repeat the artifact's full findings in multiple fields. The controller separately attaches the exact bound spec_revision, spec_sha256 and baseline_commit and labels that metadata as controller-authored. Never use partial as an outcome. Return one complete JSON object without surrounding prose. Existing full reports remain supported; neither raw receipts nor prior verdicts are rewritten. This update is delivered once; routine continuation remains only Continue. or actual new requirements.'''


def project(directory, state, request, report):
    """Never repair syntax, alter judgment fields, infer verdicts, or override identities."""
    from ao_project_room import digest
    import ao_workflow
    if 'report_format' not in report:
        return report
    if report['report_format'] != FORMAT:
        raise RoomError('Unsupported native engineering report format')
    if PART not in ao_workflow.delivered(state, request['session_id'], directory)['parts']:
        raise RoomError('Compact report contract was not delivered to this native session')
    if 'controller_metadata' in report:
        raise RoomError('The model cannot supply controller-owned report provenance')
    handoff = ao_workflow.handoff_record(directory, state)
    metadata = {key: handoff[key] for key in ('spec_revision', 'spec_sha256', 'baseline_commit')}
    if any(key in report and (report[key] != value or type(report[key]) is not type(value)) for key, value in metadata.items()):
        raise RoomError('Native report contradicts the exact bound metadata')
    return {**report, **metadata, 'controller_metadata': {
        'authored_by': 'project_room_controller', 'fields': sorted(metadata),
        'native_report_sha256': digest(report), 'receipt_sha256': request['receipt_sha256'],
        'handoff_sha256': request['handoff_sha256'], 'request_id': request['request_id']}}
