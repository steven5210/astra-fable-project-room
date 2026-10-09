"""Additive, read-only lineage/deliverable projection folded from the same owned transcript rows."""

import json
import unittest
import uuid
from datetime import datetime, timezone

import ao_progress
import ao_project_room as ao
from test_ao_normal import Fixture

MODEL = Fixture.QUALIFIED_MODEL
# The only "private" path any fixture uses; the view must never carry it (or any /leakprobe run).
LEAK = '/leakprobe/secret/file.py'
REDACTED_VECTORS = (
    LEAK, 'see `' + LEAK + '` now', '<`' + LEAK + '`>', '[' + LEAK + ']', '{' + LEAK + '}', '@' + LEAK,
    '“' + LEAK + '”', '‘' + LEAK + '’', '|' + LEAK + '|', '**' + LEAK + '**', '/' + LEAK,
    'file://' + LEAK, 'cwd=' + LEAK, 'at:' + LEAK, '(' + LEAK + ')', "'" + LEAK + "'", '"' + LEAK + '"',
    'x,' + LEAK, 'x;' + LEAK, '\t' + LEAK,
    '-' + LEAK)  # a pasted unified-diff removal line loses its path just as its '+' counterpart does
PRESERVED_VECTORS = (
    'https://example.com/a/b', 'http://h/p?q=1', 'ssh://git@h/r.git', 'git@github.com:org/repo.git',
    'src/ao_progress.py', 'a/b', '3/4', 'and/or', 'R1/R2', '<path>', '`src/x.py`')
EMPTY_CLOSURE = {'completed': 0, 'enhancement_completed': 0, 'required_open': 0, 'enhancement_open': 0,
                 'superseded': 0, 'blockers': 0, 'conflicts': 0, 'stale_in_progress': 0,
                 'window': {'created': 0, 'completed': 0, 'superseded': 0}}
EMPTY_UNMAPPED = {'count': 0, 'metadata_unavailable': 0, 'ids': [], 'superseded': 0, 'required_open': 0,
                  'completed': 0, 'blockers': 0}


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _identity():
    return str(uuid.uuid4())


def _use(tool_use_id, name, tool_input):
    return {'type': 'tool_use', 'id': tool_use_id, 'name': name, 'input': tool_input}


def _assistant(stamp, content, session, workspace, model=MODEL, stop='tool_use'):
    message = {'role': 'assistant', 'model': model, 'id': _identity(), 'content': content}
    if stop is not None:
        message['stop_reason'] = stop
    return {'type': 'assistant', 'uuid': _identity(), 'sessionId': session, 'timestamp': stamp,
            'cwd': workspace, 'isSidechain': False, 'message': message}


def _result(stamp, tool_use_id, text, session, workspace, is_error=False):
    return {'type': 'user', 'uuid': _identity(), 'sessionId': session, 'timestamp': stamp,
            'cwd': workspace, 'isSidechain': False,
            'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': text, 'is_error': is_error}]}}


class DeliverablesFixture(Fixture):
    """A bound, implementing room with helpers to append synthetic TaskCreate/TaskUpdate/TaskList rows."""

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')
        self.request = self.state()['requests']['impl-1']
        self.workspace = self.native_row_workspace()
        self.session = self.NATIVE
        self.base = self.request['created_at'] + 1.0
        self._tick = 0
        self._seq = 0

    def _next_id(self, prefix):
        self._seq += 1
        return prefix + '-' + str(self._seq)

    def at(self, offset=None):
        if offset is None:
            self._tick += 1
            offset = self._tick / 100.0
        return _iso(self.base + offset)

    def create(self, task_id, subject, metadata=None, description=None, stamp=None, active_form=None):
        stamp = stamp or self.at()
        use_id = self._next_id('tu-create-' + task_id)
        given = {'subject': subject}
        if active_form is not None:
            given['activeForm'] = active_form
        if metadata is not None:
            given['metadata'] = metadata
        if description is not None:
            given['description'] = description
        self.native_events.extend([
            _assistant(stamp, [_use(use_id, 'TaskCreate', given)], self.session, self.workspace),
            _result(stamp, use_id, 'Task #' + task_id + ' created successfully', self.session, self.workspace)])
        return stamp

    def update(self, task_id, status=None, metadata=None, description=None, stamp=None, active_form=None,
               success=True):
        stamp = stamp or self.at()
        use_id = self._next_id('tu-update-' + task_id)
        given = {'taskId': task_id}
        if status is not None:
            given['status'] = status
        if active_form is not None:
            given['activeForm'] = active_form
        if metadata is not None:
            given['metadata'] = metadata
        if description is not None:
            given['description'] = description
        text = ('Task #' + task_id + ' updated successfully') if success else 'update refused'
        self.native_events.extend([
            _assistant(stamp, [_use(use_id, 'TaskUpdate', given)], self.session, self.workspace),
            _result(stamp, use_id, text, self.session, self.workspace, is_error=not success)])
        return stamp

    def task_list(self, lines, stamp=None):
        stamp = stamp or self.at()
        use_id = self._next_id('tu-list')
        self.native_events.extend([
            _assistant(stamp, [_use(use_id, 'TaskList', {})], self.session, self.workspace),
            _result(stamp, use_id, '\n'.join(lines), self.session, self.workspace)])
        return stamp

    def todo_write(self, todos, stamp=None):
        stamp = stamp or self.at()
        use_id = self._next_id('tu-todo')
        self.native_events.append(
            _assistant(stamp, [_use(use_id, 'TodoWrite', {'todos': todos})], self.session, self.workspace))
        return stamp

    def say(self, text, stamp=None):
        stamp = stamp or self.at()
        self.native_events.append(_assistant(stamp, [{'type': 'text', 'text': text}], self.session,
                                             self.workspace, stop='end_turn'))
        return stamp

    def launch(self, description, stamp=None):
        stamp = stamp or self.at()
        use_id = self._next_id('tu-agent')
        self.native_events.extend([
            _assistant(stamp, [_use(use_id, 'Agent', {'subagent_type': 'Explore', 'description': description})],
                       self.session, self.workspace),
            _result(stamp, use_id, 'worker handback', self.session, self.workspace)])
        return stamp

    def view(self, **kwargs):
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in self.native_events))
        return ao_progress.inspect(self.directory(), self.state(), **kwargs)

    def assert_unavailable(self, deliverables):
        """available False forces the zero/empty closure, window, lists, totals and unmapped keys (C7)."""
        self.assertFalse(deliverables['available'])
        self.assertEqual(deliverables['closure'], EMPTY_CLOSURE)
        self.assertEqual(deliverables['requirements'], [])
        self.assertEqual(deliverables['unmapped'], EMPTY_UNMAPPED)
        for key in ('superseded', 'blockers', 'conflicts'):
            self.assertEqual(deliverables[key], [])
            self.assertEqual(deliverables[key + '_total'], 0)

    def assert_reconciles(self, deliverables):
        """The C6 identities (a)-(f), plus each requirement's units_total, read from one inspect() view."""
        closure, unmapped = deliverables['closure'], deliverables['unmapped']
        requirements = deliverables['requirements']
        self.assertEqual(closure['superseded'],
                         sum(entry['superseded'] for entry in requirements) + unmapped['superseded'])  # (a)
        self.assertEqual(closure['required_open'] + closure['enhancement_open'],
                         sum(entry['counts']['pending'] + entry['counts']['in_progress'] + entry['counts']['unknown']
                             for entry in requirements) + unmapped['required_open'])  # (b)
        self.assertEqual(closure['completed'] + closure['enhancement_completed'],
                         sum(entry['counts']['completed'] for entry in requirements) + unmapped['completed'])  # (c)
        self.assertEqual(closure['blockers'],
                         sum(entry['blockers'] for entry in requirements) + unmapped['blockers'])  # (d)
        self.assertEqual(closure['conflicts'], deliverables['conflicts_total'])  # (e)
        self.assertEqual(closure['window']['superseded'], closure['superseded'])  # (f)
        self.assertEqual(closure['superseded'], deliverables['superseded_total'])
        self.assertEqual(closure['blockers'], deliverables['blockers_total'])
        for entry in requirements:
            self.assertEqual(entry['units_total'], sum(entry['counts'].values()) + entry['superseded'])


class LineageTests(DeliverablesFixture):
    def test_metadata_and_description_tokens_with_metadata_precedence(self):
        self.create('1', 'R1-1: build the parser', metadata={'req': 'R1', 'kind': 'defect', 'from': 'R0'},
                    description='req=R9 note text')
        self.create('2', 'R1-2: second unit',
                    description='req=R2 kind=proof_gap from=R0 reason=blocked on review kind=defect')
        view = self.view()
        self.assertTrue(view['deliverables']['available'])
        labels = {entry['label']: entry for entry in view['deliverables']['requirements']}
        self.assertIn('R1', labels)
        self.assertEqual(labels['R1']['units'][0]['kind'], 'defect')
        self.assertEqual(labels['R1']['units'][0]['from'], 'R0')
        self.assertIn('R2', labels)
        unit2 = labels['R2']['units'][0]
        self.assertEqual(unit2['kind'], 'proof_gap')
        self.assertEqual(unit2['from'], 'R0')
        # reason= consumed the rest of its line, swallowing the trailing "kind=defect" token.
        self.assertNotIn('R9', labels)

    def test_task_update_metadata_merge_and_null_delete(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'from': 'R0', 'kind': 'defect'})
        self.update('1', metadata={'from': None, 'kind': 'proof_gap'})
        view = self.view()
        unit = view['deliverables']['requirements'][0]['units'][0]
        self.assertEqual(unit['kind'], 'proof_gap')
        self.assertIsNone(unit['from'])

    def test_completed_unit_counts_under_its_requirement_and_window(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.update('1', status='completed')
        view = self.view()
        requirement = view['deliverables']['requirements'][0]
        self.assertEqual(requirement['label'], 'R1')
        self.assertEqual(requirement['counts']['completed'], 1)
        self.assertEqual(view['deliverables']['closure']['completed'], 1)
        self.assertEqual(view['deliverables']['closure']['window']['completed'], 1)
        self.assertEqual(view['deliverables']['closure']['window']['created'], 1)

    def test_superseded_pending_unit_is_excluded_from_completed_and_required_open(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        # The successor must be an observed task id (B2): created, and marked completed so it
        # does not itself perturb this test's native pending-count assertions below.
        self.create('9', 'R1-9: successor', metadata={'req': 'R1'})
        self.update('9', status='completed')
        self.update('1', metadata={'superseded_by': '#9', 'reason': 'umbrella split'})
        view = self.view()
        deliverables = view['deliverables']
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['superseded'], 1)
        self.assertEqual(requirement['required_open'], 0)
        # A superseded row is counted only under superseded (C6); unit 9 is the one completed row.
        self.assertEqual(requirement['counts']['pending'], 0)
        self.assertEqual(requirement['counts']['completed'], 1)
        self.assertEqual(len(deliverables['superseded']), 1)
        superseded = deliverables['superseded'][0]
        self.assertEqual(superseded['id'], '1')
        self.assertEqual(superseded['successor'], '9')
        self.assertEqual(superseded['reason'], 'umbrella split')
        self.assertEqual(superseded['status'], 'pending')
        self.assertEqual(deliverables['superseded_total'], 1)
        self.assertEqual(deliverables['closure']['superseded'], 1)
        self.assertEqual(deliverables['closure']['window']['superseded'], 1)
        # The native plan keeps counting the step as pending regardless of supersession.
        self.assertEqual(view['plan']['counts']['pending'], 1)

    def test_enhancement_kind_counts_separately_from_required_open(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'kind': 'enhancement'})
        view = self.view()
        requirement = view['deliverables']['requirements'][0]
        self.assertEqual(requirement['enhancement_open'], 1)
        self.assertEqual(requirement['required_open'], 0)
        self.assertEqual(view['deliverables']['closure']['enhancement_open'], 1)
        self.assertEqual(view['deliverables']['closure']['required_open'], 0)

    def test_more_than_64_tasks_bounds_native_plan_and_sums_deliverables(self):
        for index in range(70):
            task_id = str(100 + index)
            self.create(task_id, 'R1-' + task_id + ': unit', metadata={'req': 'R1'})
        view = self.view()
        self.assertEqual(view['plan']['total'], 70)
        self.assertEqual(view['plan']['shown'], 64)
        requirement = view['deliverables']['requirements'][0]
        self.assertEqual(requirement['label'], 'R1')
        self.assertEqual(sum(requirement['counts'].values()), 70)
        self.assertEqual(requirement['units_total'], 70)
        self.assertEqual(len(requirement['units']), 16)
        self.assertTrue(view['deliverables']['limits']['truncated'])

    def test_task_list_only_ids_are_unmapped_with_metadata_unavailable(self):
        self.task_list(['#500 [pending] R1 looking task'])
        view = self.view()
        unmapped = view['deliverables']['unmapped']
        self.assertEqual(unmapped['count'], 1)
        self.assertEqual(unmapped['metadata_unavailable'], 1)
        self.assertEqual(unmapped['ids'], ['500'])
        self.assertEqual(view['deliverables']['requirements'], [])

    def test_stale_in_progress_matches_the_shown_steps_rule(self):
        old_stamp = _iso(self.base - 4000)
        self.create('1', 'UNIT-1: stuck', metadata={'req': 'R1'}, stamp=old_stamp)
        self.update('1', status='in_progress', stamp=old_stamp)
        self.create('2', 'UNIT-2: fresh', metadata={'req': 'R1'})
        self.update('2', status='in_progress')
        view = self.view()
        requirement = view['deliverables']['requirements'][0]
        units = {unit['id']: unit for unit in requirement['units']}
        self.assertTrue(units['1']['stale_in_progress'])
        self.assertFalse(units['2']['stale_in_progress'])
        self.assertEqual(view['deliverables']['closure']['stale_in_progress'], 1)
        # The same rule the operator sees in the bounded native steps (no activeForm was set,
        # so the native label falls back to the full subject text).
        plan_steps = {step['label']: step['stale_in_progress'] for step in view['plan']['steps']}
        self.assertTrue(plan_steps['UNIT-1: stuck'])
        self.assertFalse(plan_steps['UNIT-2: fresh'])

    def test_blockers_from_metadata_true_and_description_token(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'blocker': True})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1'},
                    description='blocker=true reason=waiting on decision')
        view = self.view()
        deliverables = view['deliverables']
        self.assertEqual(deliverables['blockers_total'], 2)
        reasons = {entry['id']: entry['reason'] for entry in deliverables['blockers']}
        # Unit 1's metadata blocker is the bare "True" marker (str(True)) with no reason=
        # text of its own, so the blockers list shows no reason text for it (B1).
        self.assertIsNone(reasons['1'])
        self.assertEqual(reasons['2'], 'waiting on decision')
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['blockers'], 2)
        self.assertEqual(deliverables['closure']['blockers'], 2)
        # Both tokens on unit 2's single description line parsed independently: blocker=true
        # (a bare marker, not a textual reason) and reason=... (the human-readable text),
        # rather than one token swallowing the other.
        unit2 = next(unit for unit in requirement['units'] if unit['id'] == '2')
        self.assertEqual(unit2['blocker'], 'true')

    def test_reason_token_keeps_a_trailing_key_value_looking_fragment_as_part_of_its_text(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'},
                    description='blocker=true kind=defect reason=blocked on review kind=enhancement')
        view = self.view()
        requirement = view['deliverables']['requirements'][0]
        unit = requirement['units'][0]
        # The kind= token before reason= parsed normally; the trailing "kind=enhancement"
        # after reason= is swallowed whole into the reason text, not a second kind token.
        self.assertEqual(unit['kind'], 'defect')
        blocker_entry = next(entry for entry in view['deliverables']['blockers'] if entry['id'] == '1')
        self.assertEqual(blocker_entry['reason'], 'blocked on review kind=enhancement')

    def test_superseded_row_marked_completed_counts_superseded_not_completed_and_flags_a_conflict(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.create('2', 'R1-2: successor', metadata={'req': 'R1'})
        self.update('1', status='completed', metadata={'superseded_by': '#2', 'reason': 'folded into successor'})
        view = self.view()
        deliverables = view['deliverables']
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['superseded'], 1)
        # A superseded row is never counted as completed, at any level (C6).
        self.assertEqual(requirement['counts']['completed'], 0)
        self.assertEqual(deliverables['closure']['superseded'], 1)
        self.assertEqual(deliverables['closure']['completed'], 0)
        self.assertEqual(len(deliverables['superseded']), 1)
        self.assertEqual(deliverables['superseded'][0]['status'], 'completed')
        self.assertEqual(deliverables['conflicts_total'], 1)
        conflict = deliverables['conflicts'][0]
        self.assertEqual(conflict['id'], '1')
        self.assertEqual(conflict['reason'], 'completed_and_superseded')
        self.assertEqual(conflict['successor'], '2')
        self.assertEqual(deliverables['closure']['conflicts'], 1)

    def test_superseded_by_naming_an_unobserved_id_is_not_superseded_and_flags_a_conflict(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.update('1', metadata={'superseded_by': '#9', 'reason': 'umbrella split'})
        view = self.view()
        deliverables = view['deliverables']
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['superseded'], 0)
        self.assertEqual(requirement['required_open'], 1)
        self.assertEqual(deliverables['superseded'], [])
        self.assertEqual(deliverables['superseded_total'], 0)
        self.assertEqual(deliverables['closure']['superseded'], 0)
        self.assertEqual(deliverables['closure']['required_open'], 1)
        self.assertEqual(deliverables['closure']['window']['superseded'], 0)
        self.assertEqual(deliverables['conflicts_total'], 1)
        conflict = deliverables['conflicts'][0]
        self.assertEqual(conflict['id'], '1')
        self.assertEqual(conflict['reason'], 'successor_unobserved')
        self.assertEqual(conflict['successor'], '9')
        self.assertEqual(deliverables['closure']['conflicts'], 1)

    def test_enhancement_units_excluded_from_blockers_and_required_with_correct_open_and_completed_splits(self):
        self.create('s', 'R1-S: plain successor', metadata={'req': 'R1'})
        self.update('s', status='completed')
        self.create('e1', 'R1-E1: enhancement superseded', metadata={'req': 'R1', 'kind': 'enhancement'})
        self.update('e1', metadata={'superseded_by': '#s', 'reason': 'folded in'})
        self.create('e2', 'R1-E2: enhancement blocked', metadata={'req': 'R1', 'kind': 'enhancement'},
                    description='blocker=true')
        self.create('e3', 'R1-E3: enhancement done', metadata={'req': 'R1', 'kind': 'enhancement'})
        self.update('e3', status='completed')
        view = self.view()
        deliverables = view['deliverables']
        requirement = deliverables['requirements'][0]
        self.assertEqual(deliverables['blockers'], [])  # e2's blocker is an enhancement: excluded
        self.assertEqual(deliverables['blockers_total'], 0)
        self.assertEqual(requirement['blockers'], 0)
        self.assertEqual(deliverables['closure']['required_open'], 0)
        self.assertEqual(deliverables['closure']['enhancement_open'], 1)  # e2 only; e1 is superseded
        self.assertEqual(deliverables['closure']['enhancement_completed'], 1)  # e3
        self.assertEqual(deliverables['closure']['completed'], 1)  # s; e3 is enhancement, not required
        self.assertEqual(requirement['enhancement_open'], 1)
        self.assertEqual(requirement['superseded'], 1)  # e1

    def test_unmapped_open_unit_counts_toward_required_open_not_an_entry(self):
        self.create('1', 'UNIT-1: unmapped work', metadata={'kind': 'enhancement'})  # no req given
        view = self.view()
        deliverables = view['deliverables']
        self.assertEqual(deliverables['unmapped']['count'], 1)
        self.assertEqual(deliverables['unmapped']['ids'], ['1'])
        # Unknown work is never optional: an unmapped unit counts as required, never enhancement,
        # regardless of a parsed kind=enhancement token, and never appears under a requirement.
        self.assertEqual(deliverables['closure']['required_open'], 1)
        self.assertEqual(deliverables['closure']['enhancement_open'], 0)
        self.assertEqual(deliverables['requirements'], [])

    def test_native_plan_output_is_unchanged_by_lineage_metadata(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'kind': 'defect', 'blocker': True},
                    description='reason=explaining things')
        self.update('1', status='in_progress')
        with_metadata = self.view()['plan']
        # Strip every metadata/description field from the same rows in place: the native plan
        # must be byte-identical, since it never reads either field.
        for row in self.native_events:
            message = row.get('message') if isinstance(row.get('message'), dict) else {}
            for block in message.get('content') or []:
                if (isinstance(block, dict) and block.get('type') == 'tool_use'
                        and block.get('name') in ('TaskCreate', 'TaskUpdate')
                        and isinstance(block.get('input'), dict)):
                    block['input'].pop('metadata', None)
                    block['input'].pop('description', None)
        without_metadata = self.view()['plan']
        self.assertEqual(with_metadata, without_metadata)

    def test_inspect_mutates_no_state_and_reports_unavailable_without_task_rows(self):
        before = (self.directory() / 'state.json').read_bytes()
        view = self.view()
        after = (self.directory() / 'state.json').read_bytes()
        self.assertEqual(before, after)
        self.assertFalse(view['deliverables']['available'])
        self.assertEqual(view['deliverables']['requirements'], [])
        self.assertEqual(view['deliverables']['superseded'], [])
        self.assertEqual(view['deliverables']['blockers'], [])
        self.assertTrue(view['deliverables']['limits']['notes'])
        self.assert_unavailable(view['deliverables'])  # the full zero/empty contract (C7)
        self.assertIn('no task tool activity observed', view['deliverables']['limits']['notes'])

    def test_window_superseded_excludes_a_successor_deleted_after_the_disposition(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.create('9', 'R1-9: successor', metadata={'req': 'R1'})
        self.update('9', status='completed')
        self.update('1', metadata={'superseded_by': '#9', 'reason': 'umbrella split'})
        mid_view = self.view()
        self.assertEqual(mid_view['deliverables']['closure']['window']['superseded'], 1)
        self.update('9', status='deleted')
        view = self.view()
        deliverables = view['deliverables']
        self.assertEqual(deliverables['closure']['superseded'], 0)
        self.assertEqual(deliverables['closure']['window']['superseded'], 0)
        self.assertEqual(deliverables['superseded'], [])
        conflict = deliverables['conflicts'][0]
        self.assertEqual(conflict['id'], '1')
        self.assertEqual(conflict['successor'], '9')
        self.assertEqual(conflict['reason'], 'successor_unobserved')

    def test_absolute_paths_are_redacted_in_every_lineage_field(self):
        self.create('9', 'R1-9: successor', metadata={'req': 'R1'})
        self.update('9', status='completed')
        self.create('1', 'R1-1: unit', metadata={'req': '/etc/req-label', 'from': '/etc/from-label',
                                                  'kind': '/etc/kind-label',
                                                  'superseded_by': '/etc/successor-id'})
        self.create('2', 'R1-2: unit', metadata={'req': '/etc/req-label',
                                                  'superseded_by': '/etc/missing-successor'})
        self.create('3', 'R1-3: fix /etc/broken.conf', metadata={'req': 'R1', 'blocker': '/etc/blocker-path'})
        self.create('4', 'R1-4: unit', metadata={'req': 'R1'}, description='blocker=true reason=/etc/stand-in')
        self.create('5', 'R1-5: unit', metadata={'req': 'R1', 'superseded_by': '#9',
                                                  'reason': '/etc/superseded-reason'})
        self.create('/abs/weird-id', 'R1-6: path flavored task id', metadata={'req': 'R1'})
        view = self.view()['deliverables']
        path_requirement = next(r for r in view['requirements'] if r['label'] == '<path>')
        unit1 = next(u for u in path_requirement['units'] if u['id'] == '1')
        self.assertEqual(unit1['from'], '<path>')
        self.assertEqual(unit1['kind'], 'invalid')
        self.assertEqual(unit1['kind_raw'], '<path>')
        self.assertEqual(unit1['superseded_by'], '<path>')
        conflict = next(c for c in view['conflicts'] if c['id'] == '2')
        self.assertEqual(conflict['req'], '<path>')
        self.assertEqual(conflict['successor'], '<path>')
        r1_requirement = next(r for r in view['requirements'] if r['label'] == 'R1')
        unit3 = next(u for u in r1_requirement['units'] if u['id'] == '3')
        self.assertEqual(unit3['blocker'], '<path>')
        blocker3 = next(b for b in view['blockers'] if b['id'] == '3')
        self.assertEqual(blocker3['reason'], '<path>')  # blocker itself stands in; already redacted at source
        self.assertEqual(blocker3['label'], 'R1-3: fix <path>')
        blocker4 = next(b for b in view['blockers'] if b['id'] == '4')
        self.assertEqual(blocker4['reason'], '<path>')  # the bare "true" marker: reason= stands in instead
        superseded5 = next(s for s in view['superseded'] if s['id'] == '5')
        self.assertEqual(superseded5['reason'], '<path>')
        unit_with_path_id = next(u for u in r1_requirement['units'] if u['id'] == '<path>')
        self.assertEqual(unit_with_path_id['label'], 'R1-6: path flavored task id')

    def test_absolute_path_in_a_tasklist_only_id_is_redacted_in_unmapped_ids(self):
        self.task_list(['#/var/unmapped-id [pending] looked after externally'])
        view = self.view()['deliverables']
        self.assertEqual(view['unmapped']['ids'], ['<path>'])

    def test_more_than_16_unmapped_ids_bounds_with_a_note(self):
        for index in range(17):
            self.create(str(index), 'UNIT-' + str(index) + ': work')  # no req metadata at all
        view = self.view()['deliverables']
        self.assertEqual(view['unmapped']['count'], 17)
        self.assertEqual(len(view['unmapped']['ids']), 16)
        self.assertTrue(view['limits']['truncated'])
        self.assertIn('unmapped ids bounded to 16', view['limits']['notes'])

    def test_more_than_16_superseded_entries_bounds_with_a_note(self):
        self.create('s', 'R1-S: successor', metadata={'req': 'R1'})
        self.update('s', status='completed')
        for index in range(17):
            task_id = str(index)
            self.create(task_id, 'R1-' + task_id + ': unit', metadata={'req': 'R1'})
            self.update(task_id, metadata={'superseded_by': '#s'})
        view = self.view()['deliverables']
        self.assertEqual(view['superseded_total'], 17)
        self.assertEqual(len(view['superseded']), 16)
        self.assertTrue(view['limits']['truncated'])
        self.assertIn('superseded list bounded to 16', view['limits']['notes'])

    def test_more_than_16_blockers_bounds_with_a_note(self):
        for index in range(17):
            self.create(str(index), 'UNIT-' + str(index) + ': work', metadata={'req': 'R1'},
                        description='blocker=true')
        view = self.view()['deliverables']
        self.assertEqual(view['blockers_total'], 17)
        self.assertEqual(len(view['blockers']), 16)
        self.assertTrue(view['limits']['truncated'])
        self.assertIn('blockers list bounded to 16', view['limits']['notes'])

    def test_more_than_16_conflicts_bounds_with_a_note(self):
        for index in range(17):
            task_id = str(index)
            self.create(task_id, 'UNIT-' + task_id + ': work',
                        metadata={'req': 'R1', 'superseded_by': '#missing'})
        view = self.view()['deliverables']
        self.assertEqual(view['conflicts_total'], 17)
        self.assertEqual(len(view['conflicts']), 16)
        self.assertTrue(view['limits']['truncated'])
        self.assertIn('conflicts list bounded to 16', view['limits']['notes'])

    def test_overflow_requirement_labels_fold_into_other_while_a_genuine_other_requirement_keeps_its_own_entry(
            self):
        labels = ['A' + str(index) for index in range(31)] + ['other', 'zzz']
        for index, label in enumerate(labels):
            self.create(str(index), 'UNIT-' + str(index) + ': work', metadata={'req': label})
        view = self.view()['deliverables']
        self.assertTrue(view['limits']['truncated'])
        self.assertIn('requirement labels bounded to 32', view['limits']['notes'])
        requirements = view['requirements']
        self.assertEqual(len(requirements), 33)  # 32 kept labels (incl. genuine "other") + 1 folded aggregate
        others = [entry for entry in requirements if entry['label'] == 'other']
        self.assertEqual(len(others), 2)
        genuine = next(entry for entry in others if not entry['folded'])
        folded = next(entry for entry in others if entry['folded'])
        self.assertFalse(genuine['declared'])
        self.assertEqual(genuine['units_total'], 1)  # only its own unit (label literally "other")
        self.assertEqual(folded['units_total'], 1)  # only the overflowed "zzz" unit folded in
        self.assertEqual(sum(entry['units_total'] for entry in requirements), 33)

    def test_genuine_other_requirement_past_the_unit_bound_still_reports_truncation(self):
        for index in range(17):
            self.create(str(index), 'UNIT-' + str(index) + ': work', metadata={'req': 'other'})
        view = self.view()['deliverables']
        self.assertEqual(len(view['requirements']), 1)
        entry = view['requirements'][0]
        self.assertEqual(entry['label'], 'other')
        self.assertFalse(entry['folded'])
        self.assertEqual(entry['units_total'], 17)
        self.assertEqual(len(entry['units']), 16)
        self.assertTrue(view['limits']['truncated'])
        self.assertIn('requirement units bounded to 16', view['limits']['notes'])

    def test_kind_classification_unknown_absent_empty_and_bounded(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'kind': 'not_a_real_kind_' + ('x' * 40)})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1'})  # kind absent entirely
        self.create('3', 'R1-3: unit', metadata={'req': 'R1', 'kind': ''})  # empty metadata value
        self.create('4', 'R1-4: unit', metadata={'req': 'R1'}, description='kind=')  # bare token, empty value
        view = self.view()['deliverables']
        units = {unit['id']: unit for unit in view['requirements'][0]['units']}
        self.assertEqual(units['1']['kind'], 'invalid')
        self.assertEqual(len(units['1']['kind_raw']), 32)
        self.assertEqual(units['2']['kind'], 'planned')
        self.assertIsNone(units['2']['kind_raw'])
        self.assertEqual(units['3']['kind'], 'planned')
        self.assertEqual(units['4']['kind'], 'planned')

    def test_per_key_character_bounds_apply_after_redaction(self):
        self.create('s', 'R1-S: successor', metadata={'req': 'R1'})
        self.create('1', 'R1-1: unit', metadata={
            'req': 'R' * 17, 'from': 'F' * 65, 'blocker': 'B' * 121, 'superseded_by': 's',
            'reason': 'X' * 201})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': 'T' * 65})
        view = self.view()['deliverables']
        requirement = next(r for r in view['requirements'] if r['label'] == 'R' * 16)
        unit1 = requirement['units'][0]
        self.assertEqual(len(unit1['from']), 64)
        self.assertEqual(len(unit1['blocker']), 120)
        self.assertEqual(unit1['superseded_by'], 's')
        superseded_entry = view['superseded'][0]
        self.assertEqual(superseded_entry['id'], '1')
        self.assertEqual(len(superseded_entry['reason']), 200)
        requirement_r1 = next(r for r in view['requirements'] if r['label'] == 'R1')
        unit2 = requirement_r1['units'][0]
        self.assertEqual(len(unit2['superseded_by']), 64)
        self.assertEqual(unit2['superseded_by'], 'T' * 64)
        conflict = next(c for c in view['conflicts'] if c['id'] == '2')
        self.assertEqual(conflict['successor'], 'T' * 64)

    def test_null_metadata_value_deletes_a_previously_set_key(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': '#9'})
        self.create('9', 'R1-9: successor', metadata={'req': 'R1'})
        self.update('1', metadata={'superseded_by': None})
        view = self.view()['deliverables']
        unit = next(u for r in view['requirements'] for u in r['units'] if u['id'] == '1')
        self.assertIsNone(unit['superseded_by'])
        self.assertEqual(view['superseded'], [])
        self.assertEqual(view['conflicts'], [])

    def test_dict_or_list_metadata_value_is_ignored_neither_set_nor_deleted(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'from': 'R0', 'kind': 'defect'})
        self.update('1', metadata={'from': {'nested': 'value'}, 'kind': ['enhancement']})
        view = self.view()['deliverables']
        unit = view['requirements'][0]['units'][0]
        self.assertEqual(unit['from'], 'R0')  # the dict value neither overwrote nor deleted it
        self.assertEqual(unit['kind'], 'defect')  # the list value neither overwrote nor deleted it

    def test_metadata_reason_and_blocker_override_description_tokens(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'blocker': True, 'reason': 'metadata wins'},
                    description='blocker=false reason=description text')
        view = self.view()['deliverables']
        unit = view['requirements'][0]['units'][0]
        self.assertEqual(unit['blocker'], 'True')
        blocker_entry = view['blockers'][0]
        self.assertEqual(blocker_entry['reason'], 'metadata wins')

    def test_deleted_task_drops_its_step_and_lineage(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'kind': 'defect'})
        self.update('1', status='deleted')
        view = self.view()
        self.assertEqual(view['plan']['total'], 0)
        self.assertEqual(view['deliverables']['requirements'], [])
        self.assertEqual(view['deliverables']['unmapped']['count'], 0)

    def test_tasklist_omitting_an_id_drops_it_while_keeping_preserves_lineage(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'kind': 'defect'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1'})
        self.task_list(['#1 [pending] R1-1: unit'])  # omits id 2
        view = self.view()['deliverables']
        requirement = view['requirements'][0]
        unit_ids = {unit['id'] for unit in requirement['units']}
        self.assertEqual(unit_ids, {'1'})
        unit1 = requirement['units'][0]
        self.assertEqual(unit1['kind'], 'defect')  # lineage for the kept id survives the TaskList replace
        self.assertEqual(view['unmapped']['count'], 0)

    def test_taskupdate_metadata_for_a_tasklist_only_id_clears_metadata_unavailable(self):
        self.task_list(['#500 [pending] looked after externally'])
        mid = self.view()['deliverables']
        self.assertEqual(mid['unmapped']['metadata_unavailable'], 1)
        self.update('500', metadata={'req': 'R1'})
        view = self.view()['deliverables']
        self.assertEqual(view['unmapped']['metadata_unavailable'], 0)
        self.assertEqual(view['unmapped']['count'], 0)
        self.assertEqual(view['requirements'][0]['label'], 'R1')

    def test_malformed_rows_and_unparseable_values_never_raise(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.update('1', status='in_progress', stamp='not-a-real-timestamp')
        self.native_events.append({'type': 'assistant', 'uuid': _identity(), 'sessionId': self.session,
                                   'timestamp': self.at(), 'cwd': self.workspace, 'isSidechain': False,
                                   'message': 'not-a-dict'})
        self.native_events.append({'type': 'user', 'uuid': _identity(), 'sessionId': self.session,
                                   'timestamp': self.at(), 'cwd': self.workspace, 'isSidechain': False,
                                   'message': {'role': 'user', 'content': 12345}})
        bad_list_use = self._next_id('tu-badlist')
        self.native_events.extend([
            _assistant(self.at(), [_use(bad_list_use, 'TaskList', {})], self.session, self.workspace),
            _result(self.at(), bad_list_use, '{"tasks": "not-a-list"}', self.session, self.workspace)])
        bad_create_use = self._next_id('tu-badcreate')
        self.native_events.extend([
            _assistant(self.at(), [_use(bad_create_use, 'TaskCreate', {'subject': 'unit'})], self.session,
                      self.workspace),
            _result(self.at(), bad_create_use, 'not a recognizable created-task message', self.session,
                    self.workspace)])
        view = self.view()
        self.assertTrue(view['deliverables']['available'])
        unit = next(u for r in view['deliverables']['requirements'] for u in r['units'] if u['id'] == '1')
        self.assertFalse(unit['stale_in_progress'])  # the unparseable update stamp degrades, not raises

    def test_unparseable_observed_turn_started_at_degrades_without_raising(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        state = self.state()
        state['requests']['impl-1']['observed_turn'] = {'startedAt': 'not-a-real-timestamp'}
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view()
        self.assertTrue(view['deliverables']['available'])
        self.assertEqual(view['deliverables']['requirements'][0]['label'], 'R1')

    def test_transcript_unavailable_early_return_yields_available_false_with_a_note(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        state = self.state()
        del state['native_outcome_source']
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view()
        self.assertFalse(view['deliverables']['available'])
        self.assertEqual(view['deliverables']['requirements'], [])
        self.assertEqual(view['deliverables']['closure']['window'],
                         {'created': 0, 'completed': 0, 'superseded': 0})
        self.assertIn('transcript unavailable', view['deliverables']['limits']['notes'])
        self.assert_unavailable(view['deliverables'])  # the full zero/empty contract (C7)

    def test_only_refused_task_tool_calls_leave_the_projection_unavailable_and_empty(self):
        # A refused TaskUpdate and a TaskCreate whose result is an error never take effect: the plan
        # source stays none, so the whole projection is the forced zero/empty contract (C7).
        self.update('2', status='completed', metadata={'req': 'R1', 'blocker': 'true'}, success=False)
        stamp, use_id = self.at(), self._next_id('tu-refused-create')
        self.native_events.extend([
            _assistant(stamp, [_use(use_id, 'TaskCreate', {'subject': 'R1-1: unit', 'metadata': {'req': 'R1'}})],
                       self.session, self.workspace),
            _result(stamp, use_id, 'Task #1 created successfully', self.session, self.workspace, is_error=True)])
        view = self.view()
        self.assertEqual(view['plan']['source'], 'none')
        self.assert_unavailable(view['deliverables'])

    def test_reason_consumes_only_its_own_line_a_key_on_the_next_line_still_parses(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'},
                    description='blocker=true reason=blocked here\nkind=defect')
        view = self.view()['deliverables']
        unit = view['requirements'][0]['units'][0]
        self.assertEqual(unit['kind'], 'defect')  # the key on the next line still parsed
        blocker_entry = view['blockers'][0]
        self.assertEqual(blocker_entry['reason'], 'blocked here')  # reason stayed scoped to its own line

    def test_two_reason_lines_the_last_line_wins(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'},
                    description='blocker=true reason=first line\nreason=second line')
        view = self.view()['deliverables']
        blocker_entry = view['blockers'][0]
        self.assertEqual(blocker_entry['reason'], 'second line')

    def test_bare_blocker_token_is_not_a_blocker(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'}, description='blocker=')
        view = self.view()['deliverables']
        unit = view['requirements'][0]['units'][0]
        self.assertIsNone(unit['blocker'])
        self.assertEqual(view['blockers'], [])

    def test_bare_kind_token_is_planned(self):
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'}, description='kind=')
        view = self.view()['deliverables']
        unit = view['requirements'][0]['units'][0]
        self.assertEqual(unit['kind'], 'planned')


class RedactionPathTests(DeliverablesFixture):
    """C1 through inspect(): every engineer-authored string reaches the view only via the shared redactor."""

    def test_whole_view_never_carries_the_private_marker(self):  # T1
        # TaskList-only ids that are themselves paths, listed first so later creates add to the list.
        self.task_list(['#' + LEAK + ' [pending] listed `' + LEAK + '` only',
                        '#/leakprobe/listed-two [in_progress] <`/leakprobe/listed-subject`>'])
        self.create('a1', 'R1-1: fix `' + LEAK + '` now', active_form='Fixing <`' + LEAK + '`>',
                    description=('req=[/leakprobe/r] kind={/leakprobe/k} from=@/leakprobe/f '
                                 'blocker=|/leakprobe/b| superseded_by=//leakprobe/s '
                                 'reason=“/leakprobe/why”'))
        self.update('a1', status='in_progress', active_form='(' + LEAK + ')', metadata={'from': '"' + LEAK + '"'})
        self.create('a2', 'R1-2: **' + LEAK + '**', metadata={
            'req': '**/leakprobe/req**', 'kind': 'file://' + LEAK, 'from': 'cwd=' + LEAK, 'blocker': 'true',
            'reason': 'at:' + LEAK, 'superseded_by': '#/leakprobe/missing'})
        self.create('a3', 'R1-3: superseded by the listed path id', metadata={
            'req': 'R1', 'superseded_by': '#' + LEAK, 'reason': "'" + LEAK + "'"})
        self.create('a4', 'R1-4: done but superseded', metadata={'req': 'R1', 'superseded_by': '/leakprobe/listed-two'},
                    description='reason=x,' + LEAK)
        self.update('a4', status='completed', description='reason=x;' + LEAK)
        self.create('/leakprobe/created-id', 'UNIT-5: \t' + LEAK, metadata={'blocker': '\t' + LEAK},
                    description='kind=‘/leakprobe/k’ from=//leakprobe/f')
        self.create('a6', 'R2-6: loop', metadata={'req': 'R2', 'superseded_by': 'a6', 'kind': 'x,/leakprobe/k'})
        self.launch('Survey ' + LEAK + ' and |/leakprobe/other|')
        self.say('Wrote ' + LEAK + ' then file://' + LEAK + ' and //leakprobe/x and [/leakprobe/y]')
        view = self.view()
        serialized = json.dumps(view)
        self.assertNotIn('/leakprobe', serialized)
        self.assertIn('<path>', serialized)
        # Every projection the marker was routed into is present, so the scan above is meaningful.
        deliverables = view['deliverables']
        self.assertTrue(deliverables['available'])
        self.assertIn('<path>', view['last_text'])
        self.assertTrue(any('<path>' in step['label'] for step in view['plan']['steps']))
        self.assertTrue(any('<path>' in label for group in view['plan']['groups'] for label in group['active']))
        self.assertTrue(any('<path>' in launch['description'] for launch in view['launches']))
        self.assertEqual({conflict['reason'] for conflict in deliverables['conflicts']},
                         {'successor_unobserved', 'completed_and_superseded', 'superseded_cycle'})
        self.assertEqual({entry['id'] for entry in deliverables['superseded']}, {'a3', 'a4'})
        self.assertEqual(deliverables['blockers_total'], 3)
        self.assertEqual(deliverables['unmapped']['ids'], ['<path>', '<path>', '<path>'])
        self.assert_reconciles(deliverables)

    def test_every_delimiter_vector_is_redacted_in_a_subject_and_in_a_reason_token(self):  # T2
        index = 0
        for chunk in (REDACTED_VECTORS[:10], REDACTED_VECTORS[10:]):
            ids = []
            for vector in chunk:
                index += 1
                ids.append('v' + str(index))
                self.create(ids[-1], 'R1-' + str(index) + ': ' + vector, metadata={'req': 'R1'},
                            description='blocker=true reason=' + vector)
            view = self.view()
            entries = {entry['id']: entry for entry in view['deliverables']['blockers']}
            labels = [step['label'] for step in view['plan']['steps']]
            for task_id, vector in zip(ids, chunk):
                with self.subTest(vector=vector):
                    for text in (entries[task_id]['label'], entries[task_id]['reason']):
                        self.assertNotIn('/leakprobe', text)
                        self.assertIn('<path>', text)
            self.assertNotIn('/leakprobe', json.dumps(labels))
            for task_id in ids:
                self.update(task_id, status='completed')  # closed rows leave the bounded blockers list

    def test_a_url_and_relative_paths_survive_in_a_label(self):  # T2 (preserved, inspect path)
        subject = 'R1-1: see https://example.com/a/b and src/ao_progress.py, 3/4 R1/R2'
        self.create('1', subject, metadata={'req': 'R1'})
        view = self.view()
        self.assertEqual(view['deliverables']['requirements'][0]['units'][0]['label'], subject)
        self.assertIn({'label': subject, 'status': 'pending', 'stale_in_progress': False}, view['plan']['steps'])

    def test_double_slash_runs_and_file_urls_are_redacted(self):  # T3
        self.create('1', 'R1-1: //leakprobe/secret/file.py', metadata={'req': 'R1'},
                    description='blocker=true reason=file:///leakprobe/secret/file.py')
        self.say('see //leakprobe/a and file:///leakprobe/b')
        view = self.view()
        blocker = view['deliverables']['blockers'][0]
        self.assertEqual(blocker['label'], 'R1-1: <path>')
        self.assertEqual(blocker['reason'], '<path>')
        self.assertEqual(view['last_text'], 'see <path> and <path>')


class SupersessionIdentityTests(DeliverablesFixture):
    """C2/C3: raw-id identity, chain/cycle semantics and one conflict per row, through inspect()."""

    def _superseded_ids(self, deliverables):
        return sorted(entry['id'] for entry in deliverables['superseded'])

    def _conflicts(self, deliverables):
        return sorted((entry['id'], entry['reason'], entry['successor']) for entry in deliverables['conflicts'])

    def test_self_reference_is_a_cycle_conflict_that_stays_open_and_blocking(self):  # T4
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': '#1', 'blocker': 'true'})
        deliverables = self.view()['deliverables']
        closure = deliverables['closure']
        self.assertEqual((closure['superseded'], closure['window']['superseded']), (0, 0))
        self.assertEqual((closure['required_open'], closure['blockers']), (1, 1))
        self.assertEqual(deliverables['superseded'], [])
        self.assertEqual(deliverables['conflicts'],
                         [{'id': '1', 'req': 'R1', 'reason': 'superseded_cycle', 'successor': '1'}])
        self.assertEqual(deliverables['blockers'][0]['id'], '1')
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['counts'], {'pending': 1, 'in_progress': 0, 'completed': 0, 'unknown': 0})
        self.assertEqual((requirement['superseded'], requirement['required_open'], requirement['blockers']), (0, 1, 1))
        self.assertEqual(requirement['units'][0]['superseded_by'], '1')
        self.assert_reconciles(deliverables)

    def test_a_two_cycle_keeps_both_rows_open_with_one_conflict_each(self):  # T5
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': '#2'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': '#1'})
        self.update('2', status='completed')
        deliverables = self.view()['deliverables']
        self.assertEqual(self._superseded_ids(deliverables), [])
        self.assertEqual(self._conflicts(deliverables),
                         [('1', 'superseded_cycle', '2'), ('2', 'superseded_cycle', '1')])
        closure = deliverables['closure']
        # Each row keeps its own status: 1 is open, 2 is a (non-superseded) completed row.
        self.assertEqual((closure['required_open'], closure['completed'], closure['superseded']), (1, 1, 0))
        self.assert_reconciles(deliverables)

    def test_a_three_cycle_keeps_every_row_open(self):  # T5
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': '#2'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': '#3'})
        self.create('3', 'R1-3: unit', metadata={'req': 'R1', 'superseded_by': '#1'})
        deliverables = self.view()['deliverables']
        self.assertEqual(self._superseded_ids(deliverables), [])
        self.assertEqual(self._conflicts(deliverables), [('1', 'superseded_cycle', '2'),
                                                         ('2', 'superseded_cycle', '3'),
                                                         ('3', 'superseded_cycle', '1')])
        self.assertEqual(deliverables['closure']['required_open'], 3)
        self.assert_reconciles(deliverables)

    def test_a_chain_supersedes_every_row_but_its_last(self):  # T5
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': '#2'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': '#3'})
        self.create('3', 'R1-3: unit', metadata={'req': 'R1'})
        deliverables = self.view()['deliverables']
        self.assertEqual(self._superseded_ids(deliverables), ['1', '2'])
        self.assertEqual(deliverables['conflicts'], [])
        self.assertEqual((deliverables['closure']['required_open'], deliverables['closure']['superseded']), (1, 2))
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['counts']['pending'], 1)
        self.assert_reconciles(deliverables)

    def test_a_chain_into_a_cycle_supersedes_only_the_row_outside_the_cycle(self):  # T5
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': '#2'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': '#3'})
        self.create('3', 'R1-3: unit', metadata={'req': 'R1', 'superseded_by': '#2'})
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['superseded'],
                         [{'id': '1', 'successor': '2', 'reason': None, 'status': 'pending'}])
        self.assertEqual(self._conflicts(deliverables),
                         [('2', 'superseded_cycle', '3'), ('3', 'superseded_cycle', '2')])
        self.assertEqual((deliverables['closure']['required_open'], deliverables['closure']['superseded']), (2, 1))
        self.assert_reconciles(deliverables)

    def test_a_tracked_forty_character_id_is_nameable_as_a_successor(self):  # T6
        long_id = 'k' * 40
        self.create(long_id, 'R1-1: successor', metadata={'req': 'R1'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': '#' + long_id})
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['superseded'],
                         [{'id': '2', 'successor': long_id, 'reason': None, 'status': 'pending'}])
        self.assertEqual(deliverables['conflicts'], [])
        self.assert_reconciles(deliverables)

    def test_an_untracked_id_sharing_a_tracked_prefix_is_unobserved(self):  # T6
        self.create('T' * 32, 'R1-1: tracked', metadata={'req': 'R1'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': 'T' * 33})
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['superseded'], [])
        self.assertEqual(self._conflicts(deliverables), [('2', 'successor_unobserved', 'T' * 33)])
        self.assertEqual(deliverables['closure']['required_open'], 2)
        self.assert_reconciles(deliverables)

    def test_a_tracked_path_shaped_id_is_nameable_and_displayed_redacted(self):  # T6
        self.create('/leakprobe/x', 'R1-1: successor', metadata={'req': 'R1'})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': '#/leakprobe/x'})
        view = self.view()
        deliverables = view['deliverables']
        self.assertEqual(deliverables['superseded'],
                         [{'id': '2', 'successor': '<path>', 'reason': None, 'status': 'pending'}])
        self.assertEqual(deliverables['conflicts'], [])
        unit = next(unit for unit in deliverables['requirements'][0]['units'] if unit['id'] == '2')
        self.assertEqual(unit['superseded_by'], '<path>')
        self.assertNotIn('/leakprobe', json.dumps(view))
        self.assert_reconciles(deliverables)

    def test_exactly_one_leading_hash_is_removed(self):  # T6
        self.create('#7', 'R1-1: an id that literally starts with a hash', metadata={'req': 'R1'})
        self.create('2', 'R1-2: names #7', metadata={'req': 'R1', 'superseded_by': '##7'})
        self.create('3', 'R1-3: names 7', metadata={'req': 'R1'}, description='superseded_by=#7')
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['superseded'],
                         [{'id': '2', 'successor': '#7', 'reason': None, 'status': 'pending'}])
        self.assertEqual(self._conflicts(deliverables), [('3', 'successor_unobserved', '7')])
        self.assert_reconciles(deliverables)

    def test_false_empty_and_bare_successor_values_are_absent(self):  # T11
        self.create('False', 'R1-0: an id literally named False', metadata={'req': 'R1'})
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'superseded_by': False})
        self.create('2', 'R1-2: unit', metadata={'req': 'R1', 'superseded_by': ''})
        self.create('3', 'R1-3: unit', metadata={'req': 'R1', 'superseded_by': 'false'})
        self.create('4', 'R1-4: unit', metadata={'req': 'R1'}, description='superseded_by=')
        self.create('5', 'R1-5: unit', metadata={'req': 'R1', 'superseded_by': ' FALSE '})
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['superseded'], [])
        self.assertEqual(deliverables['conflicts'], [])
        self.assertEqual(deliverables['closure']['required_open'], 6)
        self.assertEqual({unit['superseded_by'] for unit in deliverables['requirements'][0]['units']}, {None})
        self.assert_reconciles(deliverables)

    def test_a_successor_omitted_by_task_list_becomes_unobserved_and_the_row_stays_open(self):  # T14
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.create('9', 'R1-9: successor', metadata={'req': 'R1'})
        self.update('1', metadata={'superseded_by': '#9'})
        self.assertEqual(self.view()['deliverables']['closure']['superseded'], 1)
        self.task_list(['#1 [pending] R1-1: unit'])  # id 9 is no longer tracked
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['superseded'], [])
        self.assertEqual(deliverables['conflicts'],
                         [{'id': '1', 'req': 'R1', 'reason': 'successor_unobserved', 'successor': '9'}])
        self.assertEqual(deliverables['closure']['required_open'], 1)
        self.assert_reconciles(deliverables)

    def test_a_todo_write_reset_leaves_a_later_successor_reference_unobserved(self):  # T15
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.create('9', 'R1-9: successor', metadata={'req': 'R1'})
        self.update('1', metadata={'superseded_by': '#9'})
        self.assertEqual(self.view()['deliverables']['closure']['superseded'], 1)
        # TodoWrite replaces the whole list and its lineage: ids 1 and 9 are no longer tracked.
        self.todo_write([{'content': 'R1-A: first', 'status': 'pending'},
                         {'content': 'R1-B: second', 'status': 'in_progress', 'activeForm': 'Working'}])
        self.update('todo-0', metadata={'superseded_by': '#1'})
        self.create('10', 'R1-10: names a reset id', metadata={'req': 'R1', 'superseded_by': '#9'})
        view = self.view()
        self.assertEqual(view['plan']['total'], 3)
        deliverables = view['deliverables']
        self.assertEqual(deliverables['superseded'], [])
        self.assertEqual(self._conflicts(deliverables),
                         [('10', 'successor_unobserved', '9'), ('todo-0', 'successor_unobserved', '1')])
        closure = deliverables['closure']
        self.assertEqual((closure['superseded'], closure['window']['superseded'], closure['required_open']), (0, 0, 3))
        self.assertEqual((deliverables['unmapped']['count'], deliverables['unmapped']['metadata_unavailable']), (2, 1))
        self.assert_reconciles(deliverables)


class ClosureCountingTests(DeliverablesFixture):
    """C4-C6: bounded ids, the status vocabulary and the reconciling counts, through inspect()."""

    def test_an_in_progress_superseded_row_leaves_required_open_and_blockers(self):  # T7
        self.create('1', 'R1-1: unit', metadata={'req': 'R1', 'blocker': 'true'})
        self.update('1', status='in_progress')
        self.create('2', 'R1-2: successor', metadata={'req': 'R1'})
        self.update('1', metadata={'superseded_by': '#2', 'reason': 'split'})
        deliverables = self.view()['deliverables']
        closure = deliverables['closure']
        self.assertEqual((closure['required_open'], closure['blockers'], closure['superseded']), (1, 0, 1))
        self.assertEqual(deliverables['blockers'], [])
        self.assertEqual(deliverables['superseded'],
                         [{'id': '1', 'successor': '2', 'reason': 'split', 'status': 'in_progress'}])
        requirement = deliverables['requirements'][0]
        self.assertEqual(requirement['counts'], {'pending': 1, 'in_progress': 0, 'completed': 0, 'unknown': 0})
        self.assertEqual((requirement['superseded'], requirement['blockers']), (1, 0))
        unit = next(unit for unit in requirement['units'] if unit['id'] == '1')
        self.assertEqual(unit['status'], 'in_progress')
        self.assert_reconciles(deliverables)

    def test_an_unmapped_superseded_row_reconciles_through_unmapped(self):  # T8
        self.create('1', 'UNIT-1: unmapped', metadata={'superseded_by': '#2', 'blocker': 'true'})
        self.create('2', 'R1-2: successor', metadata={'req': 'R1'})
        self.update('2', status='completed')
        deliverables = self.view()['deliverables']
        closure = deliverables['closure']
        self.assertEqual((closure['superseded'], closure['required_open'], closure['blockers']), (1, 0, 0))
        self.assertEqual(deliverables['unmapped'], {'count': 1, 'metadata_unavailable': 0, 'ids': ['1'],
                                                    'superseded': 1, 'required_open': 0, 'completed': 0,
                                                    'blockers': 0})
        self.assert_reconciles(deliverables)

    def test_a_superseded_row_in_an_overflow_requirement_folds_into_other(self):  # T9
        labels = ['A%02d' % index for index in range(33)]  # sorted; A32 overflows into the folded entry
        for index, label in enumerate(labels):
            self.create(str(index), 'UNIT-' + str(index) + ': work', metadata={'req': label})
        self.create('x', 'UNIT-X: superseded overflow unit', metadata={'req': 'A32', 'superseded_by': '#0'})
        deliverables = self.view()['deliverables']
        folded = next(entry for entry in deliverables['requirements'] if entry['folded'])
        self.assertEqual((folded['superseded'], folded['units_total']), (1, 2))
        self.assertEqual(folded['counts'], {'pending': 1, 'in_progress': 0, 'completed': 0, 'unknown': 0})
        self.assertEqual(deliverables['closure']['superseded'], 1)
        self.assert_reconciles(deliverables)

    def test_mixed_fixture_reconciles_every_closure_identity(self):  # T10
        self.create('r1a', 'R1-1: open blocker', metadata={'req': 'R1', 'blocker': 'true'})
        self.create('r1b', 'R1-2: working', metadata={'req': 'R1'})
        self.update('r1b', status='in_progress')
        self.create('r1c', 'R1-3: done', metadata={'req': 'R1'})
        self.update('r1c', status='completed')
        self.create('r1d', 'R1-4: split', metadata={'req': 'R1', 'superseded_by': '#r1b'})
        self.create('r1e', 'R1-5: done then split', metadata={'req': 'R1', 'superseded_by': '#r1c'})
        self.update('r1e', status='completed')
        self.create('r2a', 'R2-1: optional', metadata={'req': 'R2', 'kind': 'enhancement'})
        self.create('r2b', 'R2-2: optional done', metadata={'req': 'R2', 'kind': 'enhancement'})
        self.update('r2b', status='completed')
        self.create('r2c', 'R2-3: odd status', metadata={'req': 'R2', 'blocker': 'waiting on review'})
        self.update('r2c', status='done')
        self.create('r2d', 'R2-4: loop a', metadata={'req': 'R2', 'superseded_by': '#r2e'})
        self.create('r2e', 'R2-5: loop b', metadata={'req': 'R2', 'superseded_by': '#r2d'})
        self.create('u1', 'UNIT-1: unmapped blocker', description='blocker=true')
        self.create('u2', 'UNIT-2: unmapped done')
        self.update('u2', status='completed')
        self.create('u3', 'UNIT-3: unmapped split', metadata={'superseded_by': '#r1a'})
        self.create('u4', 'UNIT-4: unmapped dangling', metadata={'superseded_by': '#nowhere'})
        deliverables = self.view()['deliverables']
        self.assertEqual(deliverables['closure'], {
            'completed': 2, 'enhancement_completed': 1, 'required_open': 7, 'enhancement_open': 1,
            'superseded': 3, 'blockers': 3, 'conflicts': 4, 'stale_in_progress': 0,
            'window': {'created': 14, 'completed': 4, 'superseded': 3}})
        self.assertEqual(deliverables['unmapped'], {'count': 4, 'metadata_unavailable': 0,
                                                    'ids': ['u1', 'u2', 'u3', 'u4'], 'superseded': 1,
                                                    'required_open': 2, 'completed': 1, 'blockers': 1})
        requirements = {entry['label']: entry for entry in deliverables['requirements']}
        self.assertEqual(requirements['R1']['counts'], {'pending': 1, 'in_progress': 1, 'completed': 1, 'unknown': 0})
        self.assertEqual(requirements['R2']['counts'], {'pending': 3, 'in_progress': 0, 'completed': 1, 'unknown': 1})
        self.assertEqual([(entry['id'], entry['reason']) for entry in deliverables['conflicts']],
                         [('r1e', 'completed_and_superseded'), ('r2d', 'superseded_cycle'),
                          ('r2e', 'superseded_cycle'), ('u4', 'successor_unobserved')])
        self.assert_reconciles(deliverables)

    def test_a_hundred_character_id_is_bounded_to_64_in_every_id_field(self):  # T12
        a, b, c, d, e, z = ('A' * 100, 'B' * 100, 'C' * 100, 'D' * 100, 'E' * 100, 'Z' * 100)
        self.create(a, 'R1-1: long id blocker', metadata={'req': 'R1', 'blocker': 'true'})
        self.create(b, 'R1-2: superseded by the long id', metadata={'req': 'R1', 'superseded_by': '#' + a})
        self.create(c, 'R1-3: dangling long successor', metadata={'req': 'R1', 'superseded_by': z})
        self.create(d, 'UNIT-4: unmapped long id')
        self.create(e, 'R1-5: completed and superseded', metadata={'req': 'R1', 'superseded_by': a})
        self.update(e, status='completed')
        deliverables = self.view()['deliverables']
        units = {unit['id']: unit for unit in deliverables['requirements'][0]['units']}
        self.assertEqual(set(units), {'A' * 64, 'B' * 64, 'C' * 64, 'E' * 64})
        self.assertEqual(units['B' * 64]['superseded_by'], 'A' * 64)
        self.assertEqual(units['C' * 64]['superseded_by'], 'Z' * 64)
        self.assertEqual([(entry['id'], entry['successor']) for entry in deliverables['superseded']],
                         [('B' * 64, 'A' * 64), ('E' * 64, 'A' * 64)])
        self.assertEqual([entry['id'] for entry in deliverables['blockers']], ['A' * 64])
        self.assertEqual([(entry['id'], entry['reason'], entry['successor']) for entry in deliverables['conflicts']],
                         [('C' * 64, 'successor_unobserved', 'Z' * 64),
                          ('E' * 64, 'completed_and_superseded', 'A' * 64)])
        self.assertEqual(deliverables['unmapped']['ids'], ['D' * 64])
        self.assertEqual(deliverables['closure']['superseded'], 2)  # identity used the raw 100-char ids
        self.assert_reconciles(deliverables)

    def test_an_unrecognized_status_is_unknown_open_and_never_completed(self):  # T13
        self.create('1', 'R1-1: unit', metadata={'req': 'R1'})
        self.update('1', status='done')
        self.create('2', 'R2-1: unit', metadata={'req': 'R2', 'blocker': 'true'})
        self.update('2', status='/leakprobe/status')
        deliverables = self.view()['deliverables']
        for requirement in deliverables['requirements']:
            self.assertEqual(requirement['counts'], {'pending': 0, 'in_progress': 0, 'completed': 0, 'unknown': 1})
            self.assertEqual(requirement['required_open'], 1)
            self.assertEqual([unit['status'] for unit in requirement['units']], ['unknown'])
        closure = deliverables['closure']
        self.assertEqual((closure['completed'], closure['required_open'], closure['blockers']), (0, 2, 1))
        self.assertNotIn('/leakprobe', json.dumps(deliverables))
        self.assert_reconciles(deliverables)


class AnchorTests(Fixture):
    def _bind_with_spec(self, content):
        self.room = self.open()
        self.service.ao_room_spec_put(self.room, 1, content, self.gates, 'Astra approves this scope')
        self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')

    def test_declared_labels_and_requirement_declared_flag(self):
        self._bind_with_spec('R1 — Lifecycle\nR2 — Lineage\nSome prose that is not a label.\n')
        state = self.state()
        request = state['requests']['impl-1']
        workspace = self.native_row_workspace()
        stamp = _iso(request['created_at'] + 1.0)
        use_id = 'tu-anchor-1'
        self.native_events.extend([
            _assistant(stamp, [_use(use_id, 'TaskCreate', {'subject': 'unit', 'metadata': {'req': 'R1'}})],
                      self.NATIVE, workspace),
            _result(stamp, use_id, 'Task #1 created successfully', self.NATIVE, workspace),
        ])
        use_id2 = 'tu-anchor-2'
        stamp2 = _iso(request['created_at'] + 1.1)
        self.native_events.extend([
            _assistant(stamp2, [_use(use_id2, 'TaskCreate', {'subject': 'unit2', 'metadata': {'req': 'R9'}})],
                      self.NATIVE, workspace),
            _result(stamp2, use_id2, 'Task #2 created successfully', self.NATIVE, workspace),
        ])
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in self.native_events))
        view = ao_progress.inspect(self.directory(), self.state())
        anchor = view['deliverables']['anchor']
        self.assertEqual(anchor['spec_record_sha256'], self.state()['spec_record_sha256'])
        self.assertEqual(anchor['spec_revision'], 1)
        self.assertEqual(anchor['declared_labels'], ['R1', 'R2'])
        labels = {entry['label']: entry['declared'] for entry in view['deliverables']['requirements']}
        self.assertTrue(labels['R1'])
        self.assertFalse(labels['R9'])

    def test_missing_spec_record_leaves_anchor_empty_with_a_note(self):
        self._bind_with_spec('R1 — Lifecycle\n')
        state = self.state()
        state['spec_record_sha256'] = '0' * 64
        ao.atomic(self.directory() / 'state.json', state)
        view = ao_progress.inspect(self.directory(), self.state())
        anchor = view['deliverables']['anchor']
        self.assertIsNone(anchor['spec_revision'])
        self.assertIsNone(anchor['spec_sha256'])
        self.assertEqual(anchor['declared_labels'], [])
        self.assertIn('spec record unavailable', view['deliverables']['limits']['notes'])

    def test_more_than_32_declared_labels_bounds_the_anchor_with_a_note(self):
        content = '\n'.join('R' + str(index) + ' - Label ' + str(index) for index in range(1, 34)) + '\n'
        self._bind_with_spec(content)
        view = ao_progress.inspect(self.directory(), self.state())
        anchor = view['deliverables']['anchor']
        self.assertEqual(len(anchor['declared_labels']), 32)
        self.assertEqual(anchor['declared_labels'][0], 'R1')
        self.assertNotIn('R33', anchor['declared_labels'])
        self.assertIn('declared labels bounded to 32', view['deliverables']['limits']['notes'])
        self.assertTrue(view['deliverables']['limits']['truncated'])

    def test_non_dict_spec_record_is_treated_as_unavailable_without_raising(self):
        self._bind_with_spec('R1 — Lifecycle\n')
        malformed = ['not', 'a', 'dict']
        ao.atomic(self.directory() / 'specs' / '1.json', malformed)
        state = self.state()
        state['spec_record_sha256'] = ao.digest(malformed)
        ao.atomic(self.directory() / 'state.json', state)
        view = ao_progress.inspect(self.directory(), self.state())
        anchor = view['deliverables']['anchor']
        self.assertIsNone(anchor['spec_revision'])
        self.assertIsNone(anchor['spec_sha256'])
        self.assertEqual(anchor['declared_labels'], [])
        self.assertIn('spec record unavailable', view['deliverables']['limits']['notes'])


class RedactionTests(unittest.TestCase):
    """Direct, fixture-free pins for the _redact boundary-character strengthening (F1)."""

    def test_redact_recognizes_a_path_directly_after_equals_colon_comma_or_semicolon(self):
        self.assertEqual(ao_progress._redact('cwd=/etc/passwd'), 'cwd=<path>')
        self.assertEqual(ao_progress._redact('path:/etc/passwd next'), 'path:<path> next')
        self.assertEqual(ao_progress._redact('a,/etc/passwd next'), 'a,<path> next')
        self.assertEqual(ao_progress._redact('a;/etc/passwd next'), 'a;<path> next')

    def test_redact_keeps_a_scheme_url_intact_even_though_colon_is_now_a_boundary(self):
        self.assertEqual(ao_progress._redact('see https://example.com/doc for details'),
                         'see https://example.com/doc for details')

    def test_every_delimiter_vector_loses_its_absolute_path(self):  # C1 helper vectors
        for vector in REDACTED_VECTORS:
            with self.subTest(vector=vector):
                out = ao_progress._redact(vector)
                self.assertNotIn('/leakprobe', out)
                self.assertIn('<path>', out)

    def test_urls_relative_paths_and_ratios_are_preserved_exactly(self):
        for vector in PRESERVED_VECTORS:
            with self.subTest(vector=vector):
                self.assertEqual(ao_progress._redact(vector), vector)

    def test_exact_redaction_shapes(self):
        cases = {
            'https://example.com/a and ' + LEAK: 'https://example.com/a and <path>',
            'see `' + LEAK + '` now': 'see `<path>` now',
            '[' + LEAK + ']': '[<path>]',
            '“' + LEAK + '”': '“<path>”',
            '/' + LEAK: '<path>',
            '-' + LEAK: '-<path>',
            'file://' + LEAK: '<path>',
            'FILE://host' + LEAK + ' next': '<path> next',
            'https://example.com/login?next=' + LEAK: 'https://example.com/login?next=<path>',
            'git+ssh://git@h/r.git and ' + LEAK: 'git+ssh://git@h/r.git and <path>',
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(ao_progress._redact(given), expected)

    def test_id_bound_constants(self):
        self.assertEqual(ao_progress.MAX_ID_CHARS, 64)
        self.assertEqual(ao_progress.MAX_SUPERSEDED_CHARS, ao_progress.MAX_ID_CHARS)


class DeliverablesUnavailableContractTests(unittest.TestCase):
    """Direct pin for F6: available=False forces closure/lists to zero regardless of extras."""

    def test_unavailable_forces_closure_and_lists_to_zero_regardless_of_extras(self):
        anchor = {'spec_record_sha256': None, 'spec_revision': None, 'spec_sha256': None,
                 'declared_labels': []}
        extra = {'steps': {'1': {'id': '1', 'subject': 'R1-1: unit', 'status': 'pending'}},
                'lineage': {'1': {'metadata': {'req': 'R1'}, 'description': None}},
                'stale': {'1': True},
                'window': {'created': 5, 'completed': 3, 'superseded': 2}}
        result = ao_progress._deliverables(False, extra, anchor, [], False,
                                           unavailable_reason='forced unavailable')
        self.assertEqual(result['closure'], {
            'completed': 0, 'enhancement_completed': 0, 'required_open': 0, 'enhancement_open': 0,
            'superseded': 0, 'blockers': 0, 'conflicts': 0, 'stale_in_progress': 0,
            'window': {'created': 0, 'completed': 0, 'superseded': 0}})
        self.assertEqual(result['requirements'], [])
        self.assertEqual(result['superseded'], [])
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(result['unmapped'], EMPTY_UNMAPPED)
        for key in ('superseded_total', 'blockers_total', 'conflicts_total'):
            self.assertEqual(result[key], 0)
        self.assertIn('forced unavailable', result['limits']['notes'])


if __name__ == '__main__':
    unittest.main()
