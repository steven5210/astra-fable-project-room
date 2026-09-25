"""Immutable preparation, cooperative locking, read-only audit, explicit recovery."""
import copy
import errno
import fcntl
import os
import re
import stat
from changeset_core import *
from changeset_fs import *
from changeset_plan import load_plan
from changeset_receipts import validate_receipt, staged_set_digest
from changeset_faults import fault

STATE_DIRS=('plans','templates','staged','sources','drafts','backups','journals','locks','receipts')
MAX_JOURNALS=256
MAX_RECORDS=256
MAX_SCRATCH=256
HEX=re.compile(r'^[0-9a-f]{64}$')
NONCE=re.compile(r'^[0-9a-f]{32}$')

def _private_dir(parent,name,create=False):
    if create:
        try:
            os.mkdir(name,0o700,dir_fd=parent); sync_dir(parent)
        except FileExistsError: pass
    fd=os.open(name,O_DIR,dir_fd=parent)
    try:
        st=os.fstat(fd)
        if st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o700: refuse('state_directory_unsafe')
        read_acl(fd)
        return fd
    except BaseException:
        os.close(fd); raise

def ensure_state(path):
    root=open_root(path,owner_only=True)
    try:
        for name in STATE_DIRS:
            fd=_private_dir(root.fd,name,True); os.close(fd)
        return root
    except BaseException:
        root.close(); raise

def validate_roots(workspace,state,input_root=None):
    roots=[workspace,state]+([input_root] if input_root else [])
    for i,a in enumerate(roots):
        for b in roots[i+1:]:
            if roots_overlap(a,b): refuse('roots_not_disjoint')
    installed=open_root(os.path.abspath(os.path.dirname(__file__)),owned=False)
    try:
        for root in [state]+([input_root] if input_root else []):
            if roots_overlap(root,installed): refuse('root_overlaps_installed_tool')
    finally: installed.close()

def open_roots(workspace_path,state_path,input_path,*,initialize=True):
    state=workspace=input_root=None
    try:
        workspace=open_root(workspace_path,owned=False)
        state=open_root(state_path,owner_only=True)
        input_root=open_root(input_path,owner_only=True)
        validate_roots(workspace,state,input_root)
        if initialize:
            for name in STATE_DIRS:
                fd=_private_dir(state.fd,name,True); os.close(fd)
        return state,workspace,input_root
    except BaseException:
        for root in (state,workspace,input_root):
            if root: root.close()
        raise

def workspace_key(dev,ino): return sha256((str(dev)+':'+str(ino)).encode('utf-8'))

def _journal_dir(state,key,create=False):
    if not isinstance(key,str) or not HEX.fullmatch(key): refuse('invalid_journal_identity')
    parent=_private_dir(state.fd,'journals',False)
    try:
        if create:
            names=bounded_names(parent,MAX_JOURNALS)
            if key not in names and len(names)>=MAX_JOURNALS: refuse('state_inventory_limit')
        return _private_dir(parent,key,create)
    finally: os.close(parent)

def _read_record(fd,name,digest_field):
    data,st=read_file_at(fd,name,MAX_FILE_BYTES)
    if st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o600: refuse('state_file_unsafe')
    obj=strict_loads(data,limit=MAX_FILE_BYTES)
    if not isinstance(obj,dict) or not isinstance(obj.get(digest_field),str) or digest_object(obj,digest_field)!=obj[digest_field]: refuse('journal_tampered')
    return obj

def _commit_record(fd,name,obj,field):
    obj=copy.deepcopy(obj); obj[field]=digest_object(obj,field)
    write_once_at(fd,name,canonical(obj).encode('utf-8'))
    return obj

def write_prepared(state,key,obj):
    fd=_journal_dir(state,key,True)
    try:
        saved=_commit_record(fd,'prepared.json',obj,'prepared_sha256')
        obj.update(saved)
        return saved['prepared_sha256']
    finally: os.close(fd)

def load_prepared(state,key):
    try: fd=_journal_dir(state,key)
    except FileNotFoundError: return None
    try:
        try: obj=_read_record(fd,'prepared.json','prepared_sha256')
        except ChangesetError as exc:
            if exc.code=='file_missing': return None
            raise
    finally: os.close(fd)
    expected={'journal_version','plan_sha256','batch_id','attempt_id','workspace','input_root','receipt_sha256','receipt_locator','staged_set_sha256','targets','prepared_sha256'}
    if set(obj)!=expected or type(obj['journal_version']) is not int or obj['journal_version']!=1 or obj['plan_sha256']!=key: refuse('journal_schema')
    if not isinstance(obj['attempt_id'],str) or not NONCE.fullmatch(obj['attempt_id']): refuse('journal_schema')
    if not isinstance(obj['receipt_sha256'],str) or not HEX.fullmatch(obj['receipt_sha256']): refuse('journal_schema')
    if obj['receipt_locator'] is not None: normalize_rel(obj['receipt_locator'])
    if not isinstance(obj['targets'],list) or not 1<=len(obj['targets'])<=MAX_TARGETS: refuse('journal_schema')
    # The independently expected plan is strictly schema-validated before any
    # journal-supplied source, root, or proposed fields can drive filesystem I/O.
    plan=load_plan(state,key)
    _verify_journal_plan({'prepared':obj},plan)
    seen=set()
    for entry in obj['targets']:
        if not isinstance(entry,dict) or set(entry)!={'target','source','proposed','staged','backup_sha256','no_op'}: refuse('journal_schema')
        normalize_rel(entry['target'])
        if entry['target'] in seen or type(entry['no_op']) is not bool: refuse('journal_schema')
        seen.add(entry['target']); _expected_meta(entry)
        if entry['no_op']!=(entry['source']['sha256']==entry['proposed']['sha256']): refuse('journal_schema')
        if entry['backup_sha256']!=entry['source']['sha256']: refuse('journal_schema')
        if entry['no_op']:
            if entry['staged'] is not None: refuse('journal_schema')
        else:
            _validate_stage(entry['staged'])
            if entry['staged']['metadata_digest']!=entry['source']['metadata_digest']: refuse('journal_schema')
    return obj

def _validate_stage(info):
    if not isinstance(info,dict) or set(info)!={'name','device','inode','metadata_digest','intent_id','owner_sha256'}: refuse('journal_stage_schema')
    if not isinstance(info['intent_id'],str) or not NONCE.fullmatch(info['intent_id']): refuse('journal_stage_schema')
    if info['name']!='.changeset-'+info['intent_id']+'.stage': refuse('journal_stage_schema')
    for field in ('device','inode'):
        if type(info[field]) is not int or not 0<=info[field]<(1<<64): refuse('journal_stage_schema')
    for field in ('metadata_digest','owner_sha256'):
        if not isinstance(info[field],str) or not HEX.fullmatch(info[field]): refuse('journal_stage_schema')

def _load_records(jfd,prepared=None):
    try: fd=_private_dir(jfd,'outcomes')
    except FileNotFoundError: return []
    try:
        names=bounded_names(fd,MAX_RECORDS*2)
        records=[]; previous=prepared['prepared_sha256'] if prepared else None
        for name in names:
            if name.startswith('.tmp-'): continue
            record=_read_record(fd,name,'record_sha256')
            common={'journal_version','sequence','prev_sha256','record_sha256','kind','target_index','staged','after_sha256'}
            if set(record)!=common or type(record['sequence']) is not int or record['sequence']!=len(records)+1: refuse('journal_schema')
            if type(record['journal_version']) is not int or record['journal_version']!=1 or record['kind'] not in ('stage','replace'): refuse('journal_schema')
            if record['prev_sha256']!=previous: refuse('journal_chain_mismatch')
            if name!=f"{record['sequence']:04d}-{record['record_sha256']}.json": refuse('journal_chain_mismatch')
            if type(record['target_index']) is not int or prepared is None or not 0<=record['target_index']<len(prepared['targets']): refuse('journal_schema')
            target=prepared['targets'][record['target_index']]
            if target['no_op'] or record['after_sha256']!=target['proposed']['sha256']: refuse('journal_schema')
            _validate_stage(record['staged'])
            records.append(record); previous=record['record_sha256']
            if len(records)>MAX_RECORDS: refuse('state_inventory_limit')
        return records
    finally: os.close(fd)

def read_chain(state,key):
    prepared=load_prepared(state,key)
    if prepared is None: return None
    fd=_journal_dir(state,key)
    try:
        records=_load_records(fd,prepared)
        entries=copy.deepcopy(prepared['targets']); replaced=set()
        for record in records:
            idx=record['target_index']
            if record['kind']=='stage':
                if idx in replaced: refuse('journal_chain_mismatch')
                entries[idx]['staged']=record['staged']
            else:
                if idx in replaced or entries[idx]['staged']!=record['staged']: refuse('journal_chain_mismatch')
                replaced.add(idx)
        try: completion=_read_record(fd,'completion.json','completion_sha256')
        except ChangesetError as exc:
            if exc.code!='file_missing': raise
            completion=None
        if completion is not None:
            fields={'journal_version','result','plan_sha256','prepared_sha256','last_record_sha256','targets','completion_sha256'}
            expected=[{'target_index':n,'sha256':e['proposed']['sha256']} for n,e in enumerate(entries)]
            last=records[-1]['record_sha256'] if records else prepared['prepared_sha256']
            if (set(completion)!=fields or type(completion['journal_version']) is not int or completion['journal_version']!=1 or completion['result']!='completed' or
                completion['plan_sha256']!=key or completion['prepared_sha256']!=prepared['prepared_sha256'] or
                completion['last_record_sha256']!=last or not typed_equal(completion['targets'],expected)): refuse('journal_chain_mismatch')
        return {'prepared':prepared,'records':records,'completion':completion,'targets':entries}
    finally: os.close(fd)

def append_record(state,key,record):
    chain=read_chain(state,key)
    if chain is None or chain['completion'] is not None: refuse('journal_not_open')
    if len(chain['records'])>=MAX_RECORDS: refuse('state_inventory_limit')
    seq=len(chain['records'])+1
    record=dict(record,journal_version=1,sequence=seq,prev_sha256=chain['records'][-1]['record_sha256'] if chain['records'] else chain['prepared']['prepared_sha256'])
    record['record_sha256']=digest_object(record,'record_sha256')
    fd=_journal_dir(state,key)
    try:
        ofd=_private_dir(fd,'outcomes',True)
        try: write_once_at(ofd,f"{seq:04d}-{record['record_sha256']}.json",canonical(record).encode('utf-8'))
        finally: os.close(ofd)
    finally: os.close(fd)
    return record['record_sha256']

def write_completion(state,key,obj):
    chain=read_chain(state,key)
    if chain is None: refuse('journal_missing')
    if chain['completion'] is not None: refuse('already_exists')
    record={'journal_version':1,'result':'completed','plan_sha256':key,'prepared_sha256':chain['prepared']['prepared_sha256'],
            'last_record_sha256':chain['records'][-1]['record_sha256'] if chain['records'] else chain['prepared']['prepared_sha256'],
            'targets':[{'target_index':n,'sha256':e['proposed']['sha256']} for n,e in enumerate(chain['targets'])]}
    fd=_journal_dir(state,key)
    try: return _commit_record(fd,'completion.json',record,'completion_sha256')['completion_sha256']
    finally: os.close(fd)

def _open_lock(state,key,exclusive,create):
    try: locks=_private_dir(state.fd,'locks',False)
    except FileNotFoundError:
        if not create: return None
        raise
    try:
        flags=(os.O_RDWR|os.O_CREAT if exclusive and create else os.O_RDONLY)|os.O_NOFOLLOW|os.O_NONBLOCK
        try: fd=os.open(key+'.lock',flags,0o600,dir_fd=locks)
        except FileNotFoundError: return None
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o600: refuse('lock_unsafe')
            fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK,errno.EAGAIN): return 'busy'
            refuse('lock_failed')
        except BaseException:
            os.close(fd); raise
    finally: os.close(locks)

def _journals(state,workspace):
    try: fd=_private_dir(state.fd,'journals')
    except FileNotFoundError: return []
    try: names=bounded_names(fd,MAX_JOURNALS)
    finally: os.close(fd)
    matching=[]
    for name in names:
        if not HEX.fullmatch(name): refuse('unknown_state_entry')
        chain=read_chain(state,name)
        if chain is None: continue
        saved=chain['prepared']['workspace']
        if saved.get('path')==workspace.path or (saved.get('device'),saved.get('inode'))==(workspace.dev,workspace.ino): matching.append((name,chain))
    return matching

def _read_store(state,folder,digest):
    if not isinstance(digest,str) or not HEX.fullmatch(digest): refuse('invalid_content_identity')
    fd=_private_dir(state.fd,folder)
    try:
        data,st=read_file_at(fd,digest)
        if st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o600 or sha256(data)!=digest: refuse('evidence_mismatch')
        return data
    finally: os.close(fd)

def _put_store(state,folder,data):
    fd=_private_dir(state.fd,folder)
    try: write_content_addressed(fd,sha256(data),data)
    finally: os.close(fd)
    return sha256(data)

def _read_receipts(input_root,receipts,receipts_locator):
    if receipts is not None:
        if not isinstance(receipts,bytes) or len(receipts)>MAX_FILE_BYTES: refuse('invalid_receipt')
        return receipts
    if receipts_locator is None: refuse('missing_receipts')
    parts=normalize_rel(receipts_locator); fd=descend(input_root,parts[:-1])
    try: return read_file_at(fd,parts[-1])[0]
    finally: os.close(fd)

def _expected_meta(entry):
    source=entry['source']
    meta=meta_from_record(source.get('metadata'))
    if meta_digest(meta)!=source.get('metadata_digest') or meta.mode!=source.get('mode'): refuse('metadata_pin_mismatch')
    return meta

def _source_snapshot(workspace,target):
    parts=normalize_rel(target['target']); parent=descend(workspace,parts[:-1])
    try:
        fd,st=open_regular_at(parent,parts[-1])
        try:
            data,_,after=read_fd(fd); meta=snapshot_meta(fd)
            if stat_signature(st)!=stat_signature(os.fstat(fd)): refuse('changed_while_read')
            return data,after,meta
        finally: os.close(fd)
    finally: os.close(parent)

def _verify_original(workspace,entry):
    data,st,meta=_source_snapshot(workspace,entry)
    source=entry['source']
    if len(data)!=source['bytes'] or sha256(data)!=source['sha256']: refuse('source_pin_mismatch')
    if (st.st_dev,st.st_ino)!=(source['device'],source['inode']): refuse('source_identity_mismatch')
    if meta_to_record(meta)!=meta_to_record(_expected_meta(entry)): refuse('source_metadata_mismatch')
    return data

def _verify_inputs(plan,workspace,input_root,state=None):
    """Immutable evidence only; target before/after state is checked separately."""
    if state is None: refuse('missing_state_context')
    verify_root(workspace,plan['workspace'])
    input_expected=dict(plan['input_root']); input_expected.setdefault('mode',0o700)
    verify_root(input_root,input_expected,private=True); verify_root(state,plan['state_root'],private=True)
    validate_roots(workspace,state,input_root)
    retained=0; identities=set()
    for target in plan['targets']:
        _expected_meta(target)
        identities.add((target['source']['device'],target['source']['inode']))
        if len(_read_store(state,'sources',target['source']['sha256']))!=target['source']['bytes']: refuse('source_evidence_mismatch')
        draft=target['draft']; raw=_read_store(state,'drafts',draft['sha256'])
        if len(raw)!=draft['bytes']: refuse('draft_pin_mismatch')
        if draft.get('origin')=='input':
            parts=normalize_rel(draft['locator']); fd=descend(input_root,parts[:-1])
            try: actual,_,_=hash_file_at(fd,parts[-1])
            finally: os.close(fd)
            if actual!=draft['sha256']: refuse('draft_pin_mismatch')
        elif draft.get('origin')!='inline': refuse('draft_origin_missing')
        proposed=_read_store(state,'staged',target['proposed']['sha256'])
        if len(proposed)!=target['proposed']['bytes']: refuse('proposed_pin_mismatch')
        meta=_expected_meta(target)
        retained+=target['source']['bytes']+len(raw)+len(proposed)+sum(len(n)+len(v) for n,v in meta.xattrs)
        if retained>MAX_AGGREGATE_BYTES: refuse('aggregate_bytes_exceeded')
    if len(plan['pinned_inputs'])>MAX_PINNED_INPUTS: refuse('too_many_pinned_inputs')
    for item in plan['pinned_inputs']:
        root=workspace if item['root']=='workspace' else input_root
        parts=normalize_rel(item['path']); fd=descend(root,parts[:-1])
        try: digest,size,st=hash_file_at(fd,parts[-1])
        finally: os.close(fd)
        if size!=item['bytes'] or digest!=item['sha256']: refuse('input_pin_mismatch')
        if (st.st_dev,st.st_ino) in identities: refuse('input_aliases_target')

def _verify_journal_plan(chain,plan):
    prepared=chain['prepared']
    if prepared['plan_sha256']!=plan['plan_sha256'] or prepared['batch_id']!=plan['batch_id'] or not typed_equal(prepared['workspace'],plan['workspace']): refuse('plan_journal_mismatch')
    expected_input=dict(plan['input_root']); expected_input.setdefault('mode',0o700)
    if not typed_equal(prepared['input_root'],expected_input) or prepared['staged_set_sha256']!=staged_set_digest(plan): refuse('plan_journal_mismatch')
    if len(prepared['targets'])!=len(plan['targets']): refuse('plan_journal_mismatch')
    for saved,target in zip(prepared['targets'],plan['targets']):
        if not isinstance(saved,dict) or not {'target','source','proposed'}<=set(saved): refuse('plan_journal_mismatch')
        if any(not typed_equal(saved[k],target[k]) for k in ('target','source','proposed')): refuse('plan_journal_mismatch')

def _validate_intent(intent,name,key,target_count):
    fields={'version','plan_sha256','attempt_id','target_index','name','intent_id','sha256'}
    if not isinstance(intent,dict) or set(intent)!=fields or type(intent['version']) is not int or intent['version']!=1: refuse('scratch_evidence_mismatch')
    for field in ('attempt_id','intent_id'):
        if not isinstance(intent[field],str) or not NONCE.fullmatch(intent[field]): refuse('scratch_evidence_mismatch')
    if (intent['plan_sha256']!=key or name!=intent['intent_id']+'.intent.json' or
        intent['name']!='.changeset-'+intent['intent_id']+'.stage' or type(intent['target_index']) is not int or
        not 0<=intent['target_index']<target_count): refuse('scratch_evidence_mismatch')

def _scratch_fd(state,key,create=False):
    jfd=_journal_dir(state,key,create)
    try: return _private_dir(jfd,'scratch',create)
    finally: os.close(jfd)

def _check_stage_budget(state,key,count):
    try: fd=_scratch_fd(state,key)
    except FileNotFoundError: return
    try:
        if len(bounded_names(fd,MAX_SCRATCH*3))+2*count>MAX_SCRATCH*2: refuse('state_inventory_limit')
    finally: os.close(fd)

def _stage_target(target,workspace,state,*,key=None,attempt=None,index=0):
    if key is None or attempt is None: refuse('missing_stage_context')
    proposed=_read_store(state,'staged',target['proposed']['sha256']); meta=_expected_meta(target)
    parts=normalize_rel(target['target']); parent=descend(workspace,parts[:-1])
    nonce=os.urandom(16).hex(); name='.changeset-'+nonce+'.stage'
    sfd=_scratch_fd(state,key,True)
    try:
        if len(bounded_names(sfd,MAX_SCRATCH*3))>=MAX_SCRATCH*2: refuse('state_inventory_limit')
        intent={'version':1,'plan_sha256':key,'attempt_id':attempt,'target_index':index,'name':name,'intent_id':nonce}
        _commit_record(sfd,nonce+'.intent.json',intent,'sha256')
        fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            fault('after_stage_create',target_index=index)
            apply_meta(fd,meta); st=os.fstat(fd)
            info={'name':name,'device':st.st_dev,'inode':st.st_ino,'metadata_digest':meta_digest(meta),'intent_id':nonce}
            owner=_commit_record(sfd,nonce+'.owner.json',dict(intent,stage=info),'sha256')
            info['owner_sha256']=owner['sha256']
            fault('after_stage_identity',target_index=index)
            for start in range(0,len(proposed),65536):
                write_all(fd,proposed[start:start+65536]); fault('after_stage_chunk',target_index=index)
            verify_meta(fd,meta); data,_,_=read_fd(fd)
            if data!=proposed: refuse('staged_content_mismatch')
            durability_sync(fd)
        finally: os.close(fd)
        sync_dir(parent); fault('after_stage_ready',target_index=index)
        return info
    finally: os.close(sfd); os.close(parent)

def _read_owner(state,key,info,prepared,index):
    _validate_stage(info); fd=_scratch_fd(state,key)
    try:
        owner=_read_record(fd,info['intent_id']+'.owner.json','sha256')
        intent=_read_record(fd,info['intent_id']+'.intent.json','sha256')
    finally: os.close(fd)
    expected={'version':1,'plan_sha256':key,'attempt_id':prepared['attempt_id'],'target_index':index,'name':info['name'],'intent_id':info['intent_id']}
    if not typed_equal({k:v for k,v in intent.items() if k!='sha256'},expected): refuse('scratch_evidence_mismatch')
    if set(owner)!=set(expected)|{'stage','sha256'}: refuse('scratch_evidence_mismatch')
    if not typed_equal({k:v for k,v in owner.items() if k not in ('sha256','stage')},expected): refuse('scratch_evidence_mismatch')
    if owner['sha256']!=info['owner_sha256'] or not typed_equal(owner['stage'],{k:v for k,v in info.items() if k!='owner_sha256'}): refuse('scratch_evidence_mismatch')
    return owner

def _target_state(workspace,target):
    try:
        data,st,meta=_source_snapshot(workspace,target)
        return {'sha256':sha256(data),'bytes':len(data),'device':st.st_dev,'inode':st.st_ino,'metadata_digest':meta_digest(meta)}
    except ChangesetError as exc:
        return {'error':'missing' if exc.code=='file_missing' else 'unreadable','code':exc.code}
    except OSError:
        return {'error':'unreadable','code':'file_unreadable'}

def _classify(target,current):
    if 'error' in current: return {'content':current['error'],'identity':'unknown','metadata':'unknown'}
    before,after=target['source']['sha256'],target['proposed']['sha256']
    digest=current['sha256']; original=(current['device'],current['inode'])==(target['source']['device'],target['source']['inode'])
    staged=target.get('staged'); stage_match=bool(staged and (current['device'],current['inode'])==(staged['device'],staged['inode']))
    content='unchanged' if digest==before==after else 'before' if digest==before else 'after' if digest==after else 'other'
    return {'content':content,'identity':'original' if original else 'staged' if stage_match else 'foreign',
            'metadata':'match' if current['metadata_digest']==target['source']['metadata_digest'] else 'changed'}

def _stage_state(workspace,state,key,entry,prepared,index):
    info=entry['staged']; _read_owner(state,key,info,prepared,index)
    parts=normalize_rel(entry['target']); parent=descend(workspace,parts[:-1])
    try:
        try: fd,st=open_regular_at(parent,info['name'])
        except ChangesetError as exc:
            if exc.code=='file_missing': return {'state':'missing'}
            return {'state':'unsafe','code':exc.code}
        try:
            before=stat_signature(st); data,_,_=read_fd(fd); meta=snapshot_meta(fd)
            if before!=stat_signature(os.fstat(fd)): refuse('changed_while_read')
            if (st.st_dev,st.st_ino)!=(info['device'],info['inode']) or meta_digest(meta)!=info['metadata_digest'] or meta_digest(meta)!=entry['source']['metadata_digest']: return {'state':'unsafe'}
            proposed=_read_store(state,'staged',entry['proposed']['sha256'])
            if not proposed.startswith(data): return {'state':'unsafe'}
            return {'state':'ready' if data==proposed else 'partial','sha256':sha256(data),'bytes':len(data),'device':st.st_dev,'inode':st.st_ino,'metadata_digest':meta_digest(meta)}
        finally: os.close(fd)
    finally: os.close(parent)

def _orphan_intents(workspace,state,key,chain):
    prepared=chain['prepared']; active={e['staged']['intent_id'] for e in chain['targets'] if e['staged']}
    try: fd=_scratch_fd(state,key)
    except FileNotFoundError: return []
    try:
        names=bounded_names(fd,MAX_SCRATCH*3); out=[]
        for name in names:
            if not name.endswith('.intent.json'): continue
            intent=_read_record(fd,name,'sha256')
            _validate_intent(intent,name,key,len(chain['targets']))
            if intent['intent_id'] in active: continue
            current_attempt=intent['attempt_id']==prepared['attempt_id']
            idx=intent.get('target_index')
            if type(idx) is not int or not 0<=idx<len(chain['targets']): refuse('scratch_evidence_mismatch')
            parts=normalize_rel(chain['targets'][idx]['target']); parent=descend(workspace,parts[:-1])
            try:
                try: os.stat(intent['name'],dir_fd=parent,follow_symlinks=False)
                except FileNotFoundError: continue
            finally: os.close(parent)
            try: owner=_read_record(fd,intent['intent_id']+'.owner.json','sha256')
            except ChangesetError as exc:
                if exc.code!='file_missing': raise
                out.append({'target_index':idx,'name':intent['name'],'state':'unrecorded','current_attempt':current_attempt}); continue
            if not isinstance(owner.get('stage'),dict): refuse('scratch_evidence_mismatch')
            info=dict(owner['stage'],owner_sha256=owner['sha256'])
            entry=dict(chain['targets'][idx],staged=info)
            status=_stage_state(workspace,state,key,entry,dict(prepared,attempt_id=intent['attempt_id']),idx)
            out.append({'target_index':idx,'name':info['name'],'current_attempt':current_attempt,**status})
        return out
    finally: os.close(fd)

def _evidence(plan,workspace,state,input_root,chain,receipt_bytes=None):
    _verify_journal_plan(chain,plan); _verify_inputs(plan,workspace,input_root,state)
    prepared=chain['prepared']; retained=_read_store(state,'receipts',prepared['receipt_sha256'])
    if receipt_bytes is not None and receipt_bytes!=retained: refuse('receipt_evidence_changed')
    validate_receipt(retained,plan)
    if prepared['receipt_locator'] is not None:
        if _read_receipts(input_root,None,prepared['receipt_locator'])!=retained: refuse('receipt_evidence_changed')
    for entry in chain['targets']:
        if len(_read_store(state,'backups',entry['backup_sha256']))!=entry['source']['bytes']: refuse('backup_mismatch')

def _audit_chain(workspace,state,key,chain):
    prepared=chain['prepared']; eligible=True; evidence_error=None; inp=None
    try:
        plan=load_plan(state,prepared['plan_sha256'])
        inp=open_root(prepared['input_root']['path'],owner_only=True)
        _evidence(plan,workspace,state,inp,chain)
    except (ChangesetError,OSError,KeyError,TypeError) as exc:
        eligible=False; evidence_error=getattr(exc,'code','evidence_unreadable')
    finally:
        if inp: inp.close()
    targets=[]
    for idx,entry in enumerate(chain['targets']):
        current=_target_state(workspace,entry); classification=_classify(entry,current)
        valid=classification['metadata']=='match' and ((classification['content'] in ('before','unchanged') and classification['identity']=='original') or (classification['content']=='after' and classification['identity']=='staged'))
        status=None
        if entry['staged']:
            try: status=_stage_state(workspace,state,key,entry,prepared,idx)
            except (ChangesetError,OSError) as exc: status={'state':'unsafe','code':getattr(exc,'code','staging_unreadable')}
            if classification['content']=='before' and status['state'] not in ('ready','partial'): valid=False
            if classification['content']=='after' and status['state']!='missing': valid=False
        eligible=eligible and valid
        targets.append({'target_index':idx,'observed':current,'classification':classification,'staging':status})
    try: leftovers=_orphan_intents(workspace,state,key,chain)
    except (ChangesetError,OSError) as exc:
        leftovers=[{'state':'unsafe','code':getattr(exc,'code','scratch_unreadable')}]
    if any(x['state'] not in ('ready','partial') and x.get('current_attempt',True) for x in leftovers): eligible=False
    return {'journal_sha256':prepared['prepared_sha256'],'plan_sha256':key,'record_sha256s':[r['record_sha256'] for r in chain['records']],
            'completion_sha256':chain['completion']['completion_sha256'] if chain['completion'] else None,
            'eligible':eligible,'evidence_error':evidence_error,'targets':targets,'leftover_staging':leftovers}

def _precommit_staging(workspace,state):
    """Report only paths from this tool's durable intents; never scan targets."""
    try: fd=_private_dir(state.fd,'journals')
    except FileNotFoundError: return []
    try: names=bounded_names(fd,MAX_JOURNALS)
    finally: os.close(fd)
    reports=[]
    for key in names:
        if not HEX.fullmatch(key): refuse('unknown_state_entry')
        if load_prepared(state,key) is not None: continue
        plan=load_plan(state,key); saved=plan['workspace']
        if saved['path']!=workspace.path and (saved['device'],saved['inode'])!=(workspace.dev,workspace.ino): continue
        targets=[dict(target,staged=None) for target in plan['targets']]
        pseudo={'prepared':{'attempt_id':None},'targets':targets}
        try: leftovers=_orphan_intents(workspace,state,key,pseudo)
        except (ChangesetError,OSError) as exc: leftovers=[{'state':'unsafe','code':getattr(exc,'code','scratch_unreadable')}]
        reports.append({'plan_sha256':key,'leftover_staging':leftovers})
    return reports

def _audit_locked(workspace,state):
    require_platform_support(state,readonly=True); verify_root(workspace); verify_root(state,private=True)
    journals=_journals(state,workspace)
    core={'tool':'changeset_tool','version':'changeset-tool-v1','outcome':'audited' if journals else 'no_journal','reusable_digest':True,
          'workspace_identity':{'device':workspace.dev,'inode':workspace.ino,'mode':workspace.mode},
          'journals':[_audit_chain(workspace,state,key,chain) for key,chain in journals],
          'precommit_staging':_precommit_staging(workspace,state)}
    if len(canonical(core).encode('utf-8'))>MAX_FILE_BYTES-256: refuse('audit_size_limit')
    core['audit_sha256']=digest_object(core,'audit_sha256')
    return core

def audit_workspace(workspace,state):
    validate_roots(workspace,state)
    lock=_open_lock(state,workspace_key(workspace.dev,workspace.ino),False,False)
    if lock=='busy': return {'tool':'changeset_tool','version':'changeset-tool-v1','outcome':'writer_active','reusable_digest':False}
    if lock is None:
        return {'tool':'changeset_tool','version':'changeset-tool-v1','outcome':'lock_absent','reusable_digest':False}
    try: return _audit_locked(workspace,state)
    finally: os.close(lock)

def _verify_completion(workspace,journal):
    for entry in journal['targets']:
        cls=_classify(entry,_target_state(workspace,entry))
        expected=('unchanged','original') if entry['no_op'] else ('after','staged')
        if (cls['content'],cls['identity'])!=expected or cls['metadata']!='match': refuse('completion_target_mismatch')

def _rename_target(workspace,entry):
    verify_root(workspace)
    _verify_original(workspace,entry)
    parts=normalize_rel(entry['target']); parent=descend(workspace,parts[:-1])
    try:
        info=entry['staged']; fd,st=open_regular_at(parent,info['name'])
        try:
            data,_,_=read_fd(fd); verify_meta(fd,_expected_meta(entry))
            if (st.st_dev,st.st_ino)!=(info['device'],info['inode']) or sha256(data)!=entry['proposed']['sha256']: refuse('staged_mismatch')
        finally: os.close(fd)
        # Last source check immediately precedes the per-file rename.
        _verify_original(workspace,entry)
        os.rename(info['name'],parts[-1],src_dir_fd=parent,dst_dir_fd=parent); sync_dir(parent)
        cls=_classify(entry,_target_state(workspace,entry))
        if cls!={'content':'after','identity':'staged','metadata':'match'}: refuse('rename_verification_failed')
    finally: os.close(parent)

def _record_replacement(state,key,idx,entry):
    append_record(state,key,{'kind':'replace','target_index':idx,'staged':entry['staged'],'after_sha256':entry['proposed']['sha256']})

def _complete_result(plan_sha,completion,already=False,resumed=False):
    return {'tool':'changeset_tool','version':'changeset-tool-v1','outcome':'already_completed' if already else 'resumed_unverified' if resumed else 'applied_unverified',
            'plan_sha256':plan_sha,'completion_sha256':completion,'coverage':{'mechanical':'verified','post_apply':'unknown','acceptance':'unknown'}}

def apply_plan(workspace,state,input_root,plan_sha256,receipts,receipts_locator):
    lock=_open_lock(state,workspace_key(workspace.dev,workspace.ino),True,True)
    if lock=='busy': return {'tool':'changeset_tool','version':'changeset-tool-v1','outcome':'busy','error':{'code':'workspace_busy'}}
    try:
        require_platform_support(state); plan=load_plan(state,plan_sha256)
        _verify_inputs(plan,workspace,input_root,state)
        matches=_journals(state,workspace)
        for key,chain in matches:
            if key!=plan_sha256 and chain['completion'] is None: refuse('journal_exists')
        existing=read_chain(state,plan_sha256)
        receipt_bytes=_read_receipts(input_root,receipts,receipts_locator); receipt=validate_receipt(receipt_bytes,plan)
        if existing is not None:
            if existing['completion'] is None: refuse('journal_exists')
            _evidence(plan,workspace,state,input_root,existing,receipt_bytes); _verify_completion(workspace,existing)
            return _complete_result(plan_sha256,existing['completion']['completion_sha256'],already=True)
        for target in plan['targets']: _verify_original(workspace,target)
        _check_stage_budget(state,plan_sha256,sum(t['source']['sha256']!=t['proposed']['sha256'] for t in plan['targets']))
        _put_store(state,'receipts',receipt_bytes)
        attempt=os.urandom(16).hex(); entries=[]
        for idx,target in enumerate(plan['targets']):
            source=_verify_original(workspace,target); _put_store(state,'backups',source)
            no_op=target['source']['sha256']==target['proposed']['sha256']
            staged=None if no_op else _stage_target(target,workspace,state,key=plan_sha256,attempt=attempt,index=idx)
            entries.append({k:copy.deepcopy(target[k]) for k in ('target','source','proposed')})
            entries[-1].update(staged=staged,backup_sha256=target['source']['sha256'],no_op=no_op)
        _verify_inputs(plan,workspace,input_root,state)
        for entry in entries: _verify_original(workspace,entry)
        prepared={'journal_version':1,'plan_sha256':plan_sha256,'batch_id':plan['batch_id'],'attempt_id':attempt,'workspace':plan['workspace'],
                  'input_root':dict(plan['input_root'],mode=0o700),'receipt_sha256':receipt['sha256'],'receipt_locator':receipts_locator if receipts is None else None,
                  'staged_set_sha256':staged_set_digest(plan),'targets':entries}
        for idx,entry in enumerate(entries):
            if entry['staged'] and _stage_state(workspace,state,plan_sha256,entry,prepared,idx)['state']!='ready': refuse('staged_mismatch')
        evidence={'prepared':prepared,'targets':entries}
        _evidence(plan,workspace,state,input_root,evidence,receipt_bytes)
        fault('before_prepared_write'); write_prepared(state,plan_sha256,prepared); fault('after_prepared_write')
        # Recheck the whole batch before the first live target write.
        _evidence(plan,workspace,state,input_root,evidence,receipt_bytes)
        for idx,entry in enumerate(entries):
            _verify_original(workspace,entry)
            if entry['staged'] and _stage_state(workspace,state,plan_sha256,entry,prepared,idx)['state']!='ready': refuse('staged_mismatch')
        for idx,entry in enumerate(entries):
            if entry['no_op']: continue
            fault('before_rename',target_index=idx); _rename_target(workspace,entry); fault('after_rename',target_index=idx)
            _record_replacement(state,plan_sha256,idx,entry)
        fault('before_completion'); _verify_completion(workspace,prepared)
        _evidence(plan,workspace,state,input_root,evidence,receipt_bytes)
        completion=write_completion(state,plan_sha256,{})
        return _complete_result(plan_sha256,completion)
    finally: os.close(lock)

def resume_plan(workspace,state,input_root,plan_sha256,audit_sha256,receipts,receipts_locator):
    lock=_open_lock(state,workspace_key(workspace.dev,workspace.ino),True,True)
    if lock=='busy': return {'tool':'changeset_tool','version':'changeset-tool-v1','outcome':'busy','error':{'code':'workspace_busy'}}
    try:
        require_platform_support(state,readonly=True); plan=load_plan(state,plan_sha256)
        _verify_inputs(plan,workspace,input_root,state)
        chain=read_chain(state,plan_sha256)
        if chain is None: refuse('journal_missing')
        for key,other in _journals(state,workspace):
            if key!=plan_sha256 and other['completion'] is None: refuse('journal_exists')
        audit=_audit_locked(workspace,state)
        if audit.get('audit_sha256')!=audit_sha256: refuse('stale_audit')
        receipt_bytes=_read_receipts(input_root,receipts,receipts_locator)
        _evidence(plan,workspace,state,input_root,chain,receipt_bytes)
        if chain['completion']:
            _verify_completion(workspace,chain)
            return _complete_result(plan_sha256,chain['completion']['completion_sha256'],already=True)
        report=next((r for r in audit['journals'] if r['plan_sha256']==plan_sha256),None)
        if report is None or not report['eligible']: refuse('resume_ineligible')
        work=[]
        for idx,entry in enumerate(chain['targets']):
            cls=_classify(entry,_target_state(workspace,entry))
            if cls['metadata']!='match': refuse('resume_target_mismatch')
            if entry['no_op']:
                if (cls['content'],cls['identity'])!=('unchanged','original'): refuse('resume_target_mismatch')
            elif (cls['content'],cls['identity'])==('before','original'): work.append((idx,entry))
            elif (cls['content'],cls['identity'])!=('after','staged'): refuse('resume_target_mismatch')
        # Validate every active scratch before creating any repair scratch.
        statuses={idx:_stage_state(workspace,state,plan_sha256,e,chain['prepared'],idx) for idx,e in work}
        if any(s['state'] not in ('ready','partial') for s in statuses.values()): refuse('staged_mismatch')
        repairs=sum(status['state']=='partial' for status in statuses.values())
        if len(chain['records'])+len(work)+repairs>MAX_RECORDS: refuse('state_inventory_limit')
        _check_stage_budget(state,plan_sha256,repairs)
        for idx,entry in work:
            if statuses[idx]['state']=='partial':
                info=_stage_target(entry,workspace,state,key=plan_sha256,attempt=chain['prepared']['attempt_id'],index=idx)
                append_record(state,plan_sha256,{'kind':'stage','target_index':idx,'staged':info,'after_sha256':entry['proposed']['sha256']})
                entry['staged']=info
        _evidence(plan,workspace,state,input_root,read_chain(state,plan_sha256),receipt_bytes)
        for idx,entry in work:
            _verify_original(workspace,entry)
            if _stage_state(workspace,state,plan_sha256,entry,chain['prepared'],idx)['state']!='ready': refuse('staged_mismatch')
        for idx,entry in work:
            fault('before_rename',target_index=idx); _rename_target(workspace,entry); fault('after_rename',target_index=idx)
            # A rename interrupted before outcome can be identified from the staged inode.
            _record_replacement(state,plan_sha256,idx,entry)
        current=read_chain(state,plan_sha256)
        fault('before_completion'); _verify_completion(workspace,current); _evidence(plan,workspace,state,input_root,current,receipt_bytes)
        completion=write_completion(state,plan_sha256,{})
        return _complete_result(plan_sha256,completion,resumed=True)
    finally: os.close(lock)
