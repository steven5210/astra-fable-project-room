"""The spec-review debate budget: non-accepting completed reviews, counted separately from dispatches.

Two independent counters govern Fable's exact-spec reviews. The debate budget admits ordinary
reviews while fewer than three completed reviews failed to accept; it is the product-debate limit,
never a transport or retry counter. The dispatch ceiling is a hard bound of five spec-review request
intents per room whatever their state, so failed, uncertain or otherwise undelivered dispatches still
consume room capacity. A completed review counts as accepting only when its retained verdict,
receipt, bound immutable spec record and epoch evidence all verify -- or when the authenticated
format-recovery journal holds an agreement whose source is that review, whether the chain is
active or already retained in history. Every other completed review
is debate; every non-completed intent is dispatch only.
"""

from ao_response_normalization import ResponseFormatError
from room import RoomError

DEBATE_LIMIT = 3
DISPATCH_CEILING = 5


def reviews(state):
    """Every spec_review request intent, in dispatch order."""
    return sorted((r for r in state['requests'].values() if r.get('purpose') == 'spec_review'),
                  key=lambda r: r['created_order'])


def _credits(directory, state):
    """Source reviews an authenticated format re-ask agreed; empty on damaged evidence."""
    import ao_spec_verdict_recovery
    try:
        return ao_spec_verdict_recovery.agreed_sources(directory, state)
    except (RoomError, ResponseFormatError, OSError, ValueError, TypeError, KeyError):
        return set()


def accepting(directory, state, request, credits=None):
    """Whether retained evidence proves this completed review accepted its own bound spec. Read-only; never raises."""
    import ao_engineering_model
    import ao_outcomes
    import ao_spec_verdict_recovery
    import ao_workflow
    try:
        if request.get('state') != 'completed' or request.get('role') != 'engineer':
            return False
        try:
            ao_outcomes.usable(directory, request)
            ao_engineering_model.check_request(directory, state, request)
            verdict = ao_workflow.final_json(directory, request)
            if not isinstance(verdict, dict):
                strict = False
            else:
                findings = verdict.get('findings')
                record = ao_workflow.spec_record_file(directory, request['spec_record_sha256'])
                strict = (verdict.get('decision') == 'accept'
                          and isinstance(findings, list)
                          and all(isinstance(f, str) for f in findings)
                          and not any(f.startswith('BLOCKER:') for f in findings)
                          and verdict.get('spec_revision') == record['revision']
                          and verdict.get('spec_sha256') == record['sha256'])
        except (RoomError, ResponseFormatError, OSError, ValueError, TypeError, KeyError):
            strict = False
        if strict:
            return True
        # An Astra-authenticated format re-ask makes its prose-only source review an
        # acceptance, whether its chain is active or already retained in history.
        if credits is None:
            credits = ao_spec_verdict_recovery.agreed_sources(directory, state)
        return request.get('request_id') in credits
    except (RoomError, ResponseFormatError, OSError, ValueError, TypeError, KeyError):
        return False


def counted_as(directory, state, request, credits=None):
    """'accepting', 'debate' (completed but not accepting) or 'dispatch_only' (not completed)."""
    if request.get('state') != 'completed':
        return 'dispatch_only'
    return 'accepting' if accepting(directory, state, request, credits) else 'debate'


def debate_attempts(directory, state):
    """The count of completed reviews that did not accept; the debate budget is exhausted at three."""
    credits = _credits(directory, state)
    return sum(counted_as(directory, state, r, credits) == 'debate' for r in reviews(state))


def dispatches(state):
    """Every dispatched spec-review intent counts toward the hard ceiling regardless of outcome."""
    return len(reviews(state))


def summary(directory, state):
    """Never-raising status projection of both counters; damaged evidence reports zero availability."""
    result = {'debate_attempts': None, 'debate_limit': DEBATE_LIMIT, 'dispatches': None,
              'dispatch_ceiling': DISPATCH_CEILING, 'ordinary_review_available': False, 'reviews': []}
    try:
        credits = _credits(directory, state)
        rows = [{'request_id': r.get('request_id'), 'state': r.get('state'),
                 'spec_record_sha256': r.get('spec_record_sha256'),
                 'counted_as': counted_as(directory, state, r, credits)} for r in reviews(state)]
        debate = sum(row['counted_as'] == 'debate' for row in rows)
        result.update(debate_attempts=debate, dispatches=len(rows), reviews=rows,
                      ordinary_review_available=(debate < DEBATE_LIMIT and len(rows) < DISPATCH_CEILING
                                                 and state.get('spec_review_extension') is None))
    except Exception as exc:  # A status projection reports damage; it never refuses.
        result['error'] = str(exc)[:500]
    return result
