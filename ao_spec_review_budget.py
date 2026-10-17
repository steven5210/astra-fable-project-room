"""The spec-review debate budget: non-accepting completed reviews, counted separately from dispatches.

Two independent counters govern Fable's exact-spec reviews. The debate budget admits ordinary
reviews while fewer than three completed reviews failed to accept; it is the product-debate limit,
never a transport or retry counter. The dispatch ceiling is a hard bound of five spec-review request
intents per room whatever their state, so failed, uncertain or otherwise undelivered dispatches still
consume room capacity. A re-review intent carrying spec_rereview_sha256 is a spec-review dispatch
like any other and is classified by the same rules once completed; the re-review lane's own
admission stays authoritative for its one intent and is never refused by this budget.

A completed review counts as accepting only when its retained verdict, receipt, bound immutable
spec record, bound engineer identity and epoch evidence all verify under the same strict contract
the agreement path applies -- or when the authenticated format-recovery journal holds an agreement
whose source is that review. Every other completed review is debate; every non-completed intent is
dispatch only. The classifier validates each verdict against the request's own immutable spec
record, so a review that strictly accepted an older registered revision still counts as accepting
for budget purposes while the agreement path keeps refusing it: agreement always applies to the
room's current spec record only.
"""

from ao_response_normalization import ResponseFormatError
from room import RoomError

DEBATE_LIMIT = 3
DISPATCH_CEILING = 5


def reviews(state):
    """Every spec_review request intent, in dispatch order."""
    return sorted((r for r in state['requests'].values() if r.get('purpose') == 'spec_review'),
                  key=lambda r: r['created_order'])


def accepting(directory, state, request):
    """Whether retained evidence proves this completed review accepted its own bound spec. Read-only; never raises."""
    import ao_spec_verdict_recovery
    import ao_workflow
    try:
        if request.get('state') != 'completed' or request.get('role') != 'engineer':
            return False
        try:
            record = ao_workflow.spec_record_file(directory, request['spec_record_sha256'])
            verdict = ao_workflow.strict_verdict(directory, state, request, record)
            strict = ao_workflow.accepting_verdict(verdict)
        except (RoomError, ResponseFormatError, OSError, ValueError, TypeError, KeyError, AttributeError):
            strict = False
        if strict:
            return True
        # An Astra-authenticated format recovery makes its prose-only source review an acceptance.
        chain = ao_spec_verdict_recovery.journal(directory, state)
        if not isinstance(chain, dict) or 'agreement' not in chain:
            return False
        evidence = chain.get('evidence')
        target = evidence.get('target') if isinstance(evidence, dict) else None
        return bool(isinstance(target, dict)
                    and target.get('source_request_id') == request.get('request_id'))
    except (RoomError, ResponseFormatError, OSError, ValueError, TypeError, KeyError):
        return False


def counted_as(directory, state, request):
    """'accepting', 'debate' (completed but not accepting) or 'dispatch_only' (not completed)."""
    if request.get('state') != 'completed':
        return 'dispatch_only'
    return 'accepting' if accepting(directory, state, request) else 'debate'


def debate_attempts(directory, state):
    """The count of completed reviews that did not accept; the debate budget is exhausted at three."""
    return sum(counted_as(directory, state, r) == 'debate' for r in reviews(state))


def dispatches(state):
    """Every dispatched spec-review intent counts toward the hard ceiling regardless of outcome."""
    return len(reviews(state))


def summary(directory, state):
    """Never-raising status projection of both counters; damaged evidence reports zero capacity."""
    result = {'debate_attempts': None, 'debate_limit': DEBATE_LIMIT, 'dispatches': None,
              'dispatch_ceiling': DISPATCH_CEILING, 'ordinary_review_capacity': False, 'reviews': []}
    try:
        rows = [{'request_id': r.get('request_id'), 'state': r.get('state'),
                 'spec_record_sha256': r.get('spec_record_sha256'),
                 'counted_as': counted_as(directory, state, r)} for r in reviews(state)]
        debate = sum(row['counted_as'] == 'debate' for row in rows)
        result.update(debate_attempts=debate, dispatches=len(rows), reviews=rows,
                      ordinary_review_capacity=(debate < DEBATE_LIMIT and len(rows) < DISPATCH_CEILING
                                                and state.get('spec_review_extension') is None
                                                and state.get('spec_rereview') is None))
    except Exception as exc:  # A status projection reports damage; it never refuses.
        result['error'] = str(exc)[:500]
    return result
