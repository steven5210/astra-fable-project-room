"""Stage actual new operating instructions without waking a paused native session."""
from room import RoomError


def stage(service, directory, state, request_id, message, authorization):
    from ao_project_room import atomic, digest, identifier, nonempty, read
    import ao_workflow
    identifier(request_id); nonempty(message, 'message', 6000); nonempty(authorization, 'authorization', 6000)
    service.quiet(state)
    if not ao_workflow.normal(state) or 'engineer' not in state['bindings']:
        raise RoomError('Operating amendments require a bound normal engineer')
    value = {'version': 1, 'room_id': state['room_id'], 'session_id': state['bindings']['engineer']['session_id'],
             'request_id': request_id, 'message': message, 'authorization': authorization}
    path = 'instruction-amendments/' + request_id + '.json'
    if (directory / path).exists():
        if read(directory / path) != value:
            raise RoomError('This operating amendment already has different immutable bytes')
    else:
        atomic(directory / path, value)
    pointer = {'path': path, 'sha256': digest(value)}
    pointers = state.setdefault('instruction_amendments', [])
    if pointer not in pointers:
        if any(x['path'] == path for x in pointers):
            raise RoomError('Operating amendment pointer changed')
        pointers.append(pointer)
        service.save(directory, state)
    return {**pointer, 'staged': True, 'model_dispatch': False, 'delivery': 'next separately authorized engineer request'}


def pending(directory, state, session_id, quality=None):
    """Verified carried amendments and root-backed equivalence share one proof."""
    from ao_quality_review import pending_amendments
    return quality['pending'] if quality is not None else pending_amendments(directory, state, session_id)
