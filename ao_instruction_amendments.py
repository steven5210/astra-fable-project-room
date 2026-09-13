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


def pending(directory, state, session_id):
    from ao_project_room import digest, read, sent_message
    from ao_workflow import completed_receipt
    delivered = set()
    for request in state['requests'].values():
        if request['session_id'] != session_id or request['state'] not in ('completed', 'settled_failure'):
            continue
        names = (request.get('carried') or {}).get('instruction_amendments', [])
        if names:
            receipt = completed_receipt(directory, request)
            if receipt.get('carried_sha256') != digest(request['carried']) or not sent_message(request, receipt):
                raise RoomError('Operating amendment delivery evidence changed')
            if not isinstance(names, list) or any(not isinstance(x, str) for x in names):
                raise RoomError('Malformed operating amendment delivery')
            delivered.update(names)
    result = []
    for pointer in state.get('instruction_amendments', []):
        from ao_project_room import identifier
        if not isinstance(pointer, dict) or set(pointer) != {'path', 'sha256'} or not isinstance(pointer['path'], str):
            raise RoomError('Malformed operating amendment pointer')
        prefix = 'instruction-amendments/'
        if not pointer['path'].startswith(prefix) or not pointer['path'].endswith('.json'):
            raise RoomError('Operating amendment has no exact owned path')
        identifier(pointer['path'][len(prefix):-5])
        value = read(directory / pointer['path'])
        if (digest(value) != pointer['sha256'] or value.get('version') != 1 or value.get('room_id') != state['room_id']
                or value.get('session_id') != session_id
                or pointer['path'] != 'instruction-amendments/' + value.get('request_id', '') + '.json'):
            raise RoomError('Operating amendment evidence changed')
        if pointer['sha256'] not in delivered:
            result.append((value['message'], pointer['sha256']))
    if delivered - {p['sha256'] for p in state.get('instruction_amendments', [])}:
        raise RoomError('Delivered operating amendment has no immutable source')
    return result
