"""Bounded, descriptor-based filesystem and native metadata support."""
import ctypes
import errno
import fcntl
import hashlib
import os
import platform
import stat
import struct
from changeset_core import *
from changeset_faults import fault

SYSTEM = platform.system()
O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
O_RD = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
FS_IOC_GETFLAGS = 0x80086601 if struct.calcsize('L') == 8 else 0x80046601
LINUX_ALLOWED_FLAGS = 0x00080000  # EXTENTS: representation, verified on the sibling too.
LINUX_ACL_NAMES = (b'system.posix_acl_access', b'system.posix_acl_default')
ACL_FIRST_ENTRY, ACL_NEXT_ENTRY = 0, -1
# Darwin sys/xattr.h and getxattr(2)/listxattr(2) explicitly support this
# option on descriptors; omit it and compression attributes can be hidden.
XATTR_SHOWCOMPRESSION = 0x0020
_LINUX_PROOF = None
_PROOF_NAME = b'trusted.changeset_visibility_v1'

class Root:
    __slots__ = ('fd','path','dev','ino','mode','components','ancestors')
    def __init__(self, fd, path, dev, ino, mode, components, ancestors=()):
        self.fd,self.path,self.dev,self.ino = fd,path,dev,ino
        self.mode,self.components,self.ancestors = mode,components,ancestors
    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

def _path_components(path):
    if not isinstance(path,str) or not path.startswith('/') or '\0' in path or BACKSLASH in path:
        refuse('invalid_root_path')
    if path != os.path.normpath(path) or path.startswith('//'):
        refuse('path_not_normalized')
    parts = [] if path == '/' else path[1:].split('/')
    if len(parts)>MAX_PATH_DEPTH or any(p in ('','.','..') for p in parts):
        refuse('invalid_root_path')
    try: path.encode('utf-8','strict')
    except UnicodeError: refuse('invalid_path_encoding')
    return parts

def normalize_rel(path, *, allow_empty=False):
    if not isinstance(path,str) or path.startswith('/') or '\0' in path or BACKSLASH in path:
        refuse('invalid_relative_path')
    parts = () if path == '' else tuple(path.split('/'))
    if (not parts and not allow_empty) or len(parts)>MAX_PATH_DEPTH or any(p in ('','.','..') for p in parts):
        refuse('invalid_relative_path')
    try: path.encode('utf-8','strict')
    except UnicodeError: refuse('invalid_path_encoding')
    return parts

def open_root(path, *, owner_only=False, owned=True):
    parts = _path_components(path)
    fd = os.open('/',O_DIR)
    ancestors=[]
    try:
        st=os.fstat(fd); ancestors.append((st.st_dev,st.st_ino))
        for part in parts:
            nfd=os.open(part,O_DIR,dir_fd=fd); os.close(fd); fd=nfd
            st=os.fstat(fd); ancestors.append((st.st_dev,st.st_ino))
        if not stat.S_ISDIR(st.st_mode): refuse('root_not_directory')
        if owned and st.st_uid!=os.geteuid(): refuse('root_not_owned')
        if owner_only and stat.S_IMODE(st.st_mode)!=0o700: refuse('root_not_private')
        if owner_only: read_acl(fd)
        return Root(fd,path,st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode),tuple(parts),tuple(ancestors))
    except BaseException:
        os.close(fd); raise

def root_record(root):
    return {'path':root.path,'device':root.dev,'inode':root.ino,'mode':root.mode}

def verify_root(root, expected=None, *, private=False):
    expected=expected or root_record(root)
    reopened=open_root(root.path,owner_only=private,owned=private)
    try:
        current=root_record(reopened)
        if any(current[k]!=expected.get(k) for k in ('path','device','inode','mode')): refuse('root_identity_mismatch')
        st=os.fstat(root.fd)
        if (st.st_dev,st.st_ino,stat.S_IMODE(st.st_mode))!=(root.dev,root.ino,root.mode): refuse('root_identity_mismatch')
    finally: reopened.close()

def roots_overlap(a,b):
    return ((a.dev,a.ino) in b.ancestors or (b.dev,b.ino) in a.ancestors or a.path==b.path or
            a.path.startswith(b.path.rstrip('/')+'/') or b.path.startswith(a.path.rstrip('/')+'/'))

def descend(root,parts):
    fd=os.dup(root.fd)
    try:
        for p in parts:
            if not isinstance(p,str) or p in ('','.','..') or '/' in p or '\0' in p or BACKSLASH in p: refuse('invalid_path_component')
            nfd=os.open(p,O_DIR,dir_fd=fd); os.close(fd); fd=nfd
        return fd
    except BaseException:
        os.close(fd); raise

def stat_signature(st):
    return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,getattr(st,'st_flags',0))
_sig=stat_signature

def open_regular_at(dir_fd,name):
    if not isinstance(name,str) or name in ('','.','..') or '/' in name or '\0' in name: refuse('invalid_file_name')
    try: fd=os.open(name,O_RD,dir_fd=dir_fd)
    except FileNotFoundError: refuse('file_missing')
    except OSError: refuse('file_unreadable')
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1: refuse('not_regular_single_link')
        return fd,st
    except BaseException:
        os.close(fd); raise

def read_fd(fd,maximum=MAX_FILE_BYTES,*,retain=True):
    before=os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: refuse('not_regular_single_link')
    if before.st_size>maximum: refuse('file_too_large')
    chunks=[]; size=0; digest=hashlib.sha256(); os.lseek(fd,0,os.SEEK_SET)
    while True:
        data=os.read(fd,min(65536,maximum-size+1))
        if not data: break
        size+=len(data)
        if size>maximum: refuse('file_too_large')
        digest.update(data)
        if retain: chunks.append(data)
    after=os.fstat(fd)
    if stat_signature(before)!=stat_signature(after): refuse('changed_while_read')
    return (b''.join(chunks) if retain else digest.hexdigest()),size,after

def read_file_at(dir_fd,name,maximum=MAX_FILE_BYTES):
    fd,_=open_regular_at(dir_fd,name)
    try:
        data,_,st=read_fd(fd,maximum); return data,st
    finally: os.close(fd)

def hash_file_at(dir_fd,name,maximum=MAX_FILE_BYTES):
    fd,_=open_regular_at(dir_fd,name)
    try: return read_fd(fd,maximum,retain=False)
    finally: os.close(fd)

def _write_all(fd,data):
    view=memoryview(data)
    while view:
        n=os.write(fd,view[:65536])
        if n<=0: refuse('short_write')
        view=view[n:]
write_all=_write_all

def durability_sync(fd):
    fault('durability_sync')
    try:
        if SYSTEM=='Darwin': fcntl.fcntl(fd,getattr(fcntl,'F_FULLFSYNC',51))
        elif SYSTEM=='Linux': os.fsync(fd)
        else: refuse('unsupported_platform')
    except OSError: refuse('unsupported_durability')

def sync_dir(fd):
    fault('sync_dir')
    try: os.fsync(fd)
    except OSError: refuse('unsupported_durability')
    if SYSTEM=='Darwin': durability_sync(fd)

def write_once_at(dir_fd,name,data,mode=0o600):
    """Atomic immutable record commit; abandoned private temporaries survive."""
    if len(data)>MAX_FILE_BYTES: refuse('record_too_large')
    try: os.stat(name,dir_fd=dir_fd,follow_symlinks=False)
    except FileNotFoundError: pass
    else: refuse('already_exists')
    tmp='.tmp-'+os.urandom(16).hex()
    fd=os.open(tmp,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode,dir_fd=dir_fd)
    try:
        os.fchmod(fd,mode); write_all(fd,data)
        fault('before_record_sync',record=name); durability_sync(fd)
    finally: os.close(fd)
    fault('before_record_commit',record=name)
    os.rename(tmp,name,src_dir_fd=dir_fd,dst_dir_fd=dir_fd)
    sync_dir(dir_fd); fault('after_record_commit',record=name)

def write_new_at(dir_fd,name,data,mode=0o600):
    try: old,st=read_file_at(dir_fd,name,len(data)+1)
    except ChangesetError as exc:
        if exc.code!='file_missing': raise
    else:
        if old!=data or st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=mode: refuse('content_address_conflict')
        return
    write_once_at(dir_fd,name,data,mode)

def write_content_addressed(dir_fd,digest,data):
    if sha256(data)!=digest: refuse('content_address_mismatch')
    write_new_at(dir_fd,digest,data)

def bounded_names(fd,maximum):
    names=[]
    with os.scandir(fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names)>maximum: refuse('state_inventory_limit')
    return sorted(names)

def _libc(): return ctypes.CDLL(None,use_errno=True)

def _native(name,args,result):
    try: fn=getattr(_libc(),name)
    except AttributeError: refuse('unsupported_metadata_primitive')
    fn.argtypes,fn.restype=args,result
    return fn

def _mac_names(fd):
    fn=_native('flistxattr',[ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int],ctypes.c_ssize_t)
    size=fn(fd,None,0,XATTR_SHOWCOMPRESSION)
    if size<0: refuse('xattr_query_failed')
    if size>MAX_XATTR_BYTES+MAX_XATTRS: refuse('xattr_bounds')
    buf=ctypes.create_string_buffer(max(1,size)); count=fn(fd,buf,size,XATTR_SHOWCOMPRESSION)
    if count<0 or count!=size: refuse('xattr_changed_while_read')
    raw=buf.raw[:count]
    if raw and not raw.endswith(b'\0'): refuse('xattr_query_failed')
    names=raw[:-1].split(b'\0') if raw else []
    if any(not n for n in names) or len(set(names))!=len(names): refuse('xattr_query_failed')
    return names

def _mac_get(fd,name,remaining):
    fn=_native('fgetxattr',[ctypes.c_int,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int],ctypes.c_ssize_t)
    size=fn(fd,name,None,0,0,XATTR_SHOWCOMPRESSION)
    if size<0: refuse('xattr_query_failed')
    if size>remaining: refuse('xattr_bounds')
    buf=ctypes.create_string_buffer(max(1,size)); count=fn(fd,name,buf,size,0,XATTR_SHOWCOMPRESSION)
    if count<0 or count!=size: refuse('xattr_changed_while_read')
    return buf.raw[:size]

def linux_cap_sys_admin(target_fd=None):
    """Current access to a retained trusted attr proves more than UID/CapEff."""
    if _LINUX_PROOF is None: return False
    fd,ident,value=_LINUX_PROOF
    try:
        st=os.fstat(fd)
        if target_fd is not None and os.fstat(target_fd).st_dev!=ident[0]: return False
        return ((st.st_dev,st.st_ino)==ident and st.st_uid==os.geteuid() and stat.S_ISREG(st.st_mode) and
                st.st_nlink==1 and stat.S_IMODE(st.st_mode)==0o600 and os.getxattr(fd,_PROOF_NAME)==value)
    except (OSError,AttributeError): return False

def _discard_visibility_temp(dir_fd,name,fd,identity):
    """Remove only this uncommitted probe while its exclusive fd stays open."""
    if identity is None: return
    try:
        current=os.fstat(fd)
        if ((current.st_dev,current.st_ino)!=identity or current.st_uid!=os.geteuid() or
            not stat.S_ISREG(current.st_mode) or current.st_nlink!=1): return
        named=os.stat(name,dir_fd=dir_fd,follow_symlinks=False)
        if stat_signature(named)!=stat_signature(current): return
        os.unlink(name,dir_fd=dir_fd)
        sync_dir(dir_fd)
    except (OSError,ChangesetError):
        # Preserve foreign or unprovable files and the original refusal if
        # best-effort cleanup or its directory sync fails.
        pass

def require_platform_support(state=None,*,readonly=False):
    global _LINUX_PROOF
    if SYSTEM=='Darwin':
        for name in ('acl_get_fd','acl_get_entry','acl_valid','acl_free','flistxattr','fgetxattr','fsetxattr','fremovexattr'):
            if not hasattr(_libc(),name): refuse('unsupported_metadata_primitive')
        return
    if SYSTEM!='Linux' or any(not hasattr(os,n) for n in ('listxattr','getxattr','setxattr','removexattr')): refuse('unsupported_platform')
    if state is None:
        if not linux_cap_sys_admin(): refuse('unsupported_capability')
        return
    name='linux-visibility.json'
    try: fd,st=open_regular_at(state.fd,name)
    except ChangesetError as exc:
        if exc.code!='file_missing' or readonly: refuse('unsupported_capability')
        bounded_names(state.fd,64)
        tmp='.visibility-'+os.urandom(16).hex()
        fd=os.open(tmp,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=state.fd)
        identity=None; committed=False
        try:
            st=os.fstat(fd); identity=(st.st_dev,st.st_ino)
            os.fchmod(fd,0o600); value=os.urandom(32)
            record={'version':1,'device':st.st_dev,'inode':st.st_ino,'value':b64e(value)}
            write_all(fd,canonical(record).encode('utf-8')); os.setxattr(fd,_PROOF_NAME,value)
            if os.getxattr(fd,_PROOF_NAME)!=value: refuse('unsupported_capability')
            durability_sync(fd); os.rename(tmp,name,src_dir_fd=state.fd,dst_dir_fd=state.fd)
            committed=True; sync_dir(state.fd)
        except BaseException as exc:
            try:
                if not committed: _discard_visibility_temp(state.fd,tmp,fd,identity)
            finally: os.close(fd)
            if isinstance(exc,OSError): refuse('unsupported_capability')
            raise
    try:
        data,_,st=read_fd(fd,4096); record=strict_loads(data,limit=4096)
        if not isinstance(record,dict) or set(record)!={'version','device','inode','value'}: refuse('unsupported_capability')
        if type(record['version']) is not int or record['version']!=1: refuse('unsupported_capability')
        if any(type(record[k]) is not int or not 0<=record[k]<(1<<64) for k in ('device','inode')): refuse('unsupported_capability')
        value=b64d(record['value'])
        if len(value)!=32 or (record['device'],record['inode'])!=(st.st_dev,st.st_ino): refuse('unsupported_capability')
        old=_LINUX_PROOF; _LINUX_PROOF=(os.dup(fd),(st.st_dev,st.st_ino),value)
        if old is not None: os.close(old[0])
        if not linux_cap_sys_admin(): refuse('unsupported_capability')
    except (OSError,ValueError,TypeError,KeyError): refuse('unsupported_capability')
    finally: os.close(fd)

def read_xattrs(fd):
    if SYSTEM=='Linux' and not linux_cap_sys_admin(fd): refuse('unsupported_capability')
    before=os.fstat(fd)
    try:
        names=_mac_names(fd) if SYSTEM=='Darwin' else [os.fsencode(n) for n in os.listxattr(fd)]
        if len(names)>MAX_XATTRS: refuse('xattr_bounds')
        out=[]; total=0
        for name in sorted(names):
            total+=len(name)
            if total>MAX_XATTR_BYTES: refuse('xattr_bounds')
            value=_mac_get(fd,name,MAX_XATTR_BYTES-total) if SYSTEM=='Darwin' else os.getxattr(fd,name)
            total+=len(value)
            if total>MAX_XATTR_BYTES: refuse('xattr_bounds')
            out.append((name,value))
        again=_mac_names(fd) if SYSTEM=='Darwin' else [os.fsencode(n) for n in os.listxattr(fd)]
        if sorted(names)!=sorted(again) or stat_signature(before)!=stat_signature(os.fstat(fd)): refuse('xattr_changed_while_read')
        if SYSTEM=='Linux' and not linux_cap_sys_admin(fd): refuse('unsupported_capability')
        return tuple(out)
    except OSError: refuse('xattr_query_failed')

def write_xattrs(fd,wanted):
    wanted=dict(wanted); current=dict(read_xattrs(fd))
    try:
        for name in current.keys()-wanted.keys():
            if SYSTEM=='Darwin':
                fn=_native('fremovexattr',[ctypes.c_int,ctypes.c_char_p,ctypes.c_int],ctypes.c_int)
                if fn(fd,name,0)!=0: refuse('xattr_preservation_failed')
            else: os.removexattr(fd,name)
        for name,value in wanted.items():
            if current.get(name)==value: continue
            if SYSTEM=='Darwin':
                fn=_native('fsetxattr',[ctypes.c_int,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int],ctypes.c_int)
                buf=ctypes.create_string_buffer(value,max(1,len(value)))
                if fn(fd,name,buf,len(value),0,0)!=0: refuse('xattr_preservation_failed')
            else: os.setxattr(fd,name,value)
    except OSError: refuse('xattr_preservation_failed')
    if dict(read_xattrs(fd))!=wanted: refuse('xattr_preservation_failed')

def read_linux_acl(fd):
    for name in LINUX_ACL_NAMES:
        try: os.getxattr(fd,name)
        except OSError as exc:
            if exc.errno==errno.ENODATA: continue
            refuse('acl_query_failed')
        else: refuse('unsupported_acl')
    return 'absent'

def macos_acl_status(fd):
    os.fstat(fd)
    getfd=_native('acl_get_fd',[ctypes.c_int],ctypes.c_void_p); ctypes.set_errno(0); acl=getfd(fd)
    if not acl:
        if ctypes.get_errno()==errno.ENOENT: return 'absent'
        refuse('acl_query_failed')
    free=_native('acl_free',[ctypes.c_void_p],ctypes.c_int)
    try:
        valid=_native('acl_valid',[ctypes.c_void_p],ctypes.c_int)
        if valid(acl)!=0: refuse('acl_query_failed')
        entry=ctypes.c_void_p()
        getentry=_native('acl_get_entry',[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_void_p)],ctypes.c_int)
        ctypes.set_errno(0); result=getentry(acl,ACL_FIRST_ENTRY,ctypes.byref(entry))
        if result==-1 and ctypes.get_errno()==errno.EINVAL: return 'absent'
        if result==0: refuse('unsupported_acl')
        refuse('acl_query_failed')
    finally: free(acl)

def read_acl(fd):
    if SYSTEM=='Darwin': return macos_acl_status(fd)
    if SYSTEM=='Linux': return read_linux_acl(fd)
    refuse('unsupported_platform')

def remove_acl(fd):
    if read_acl(fd)!='absent': refuse('unsupported_acl')
remove_linux_acl=remove_acl
remove_macos_acl=remove_acl

def read_flags(fd):
    if SYSTEM=='Linux':
        try: raw=fcntl.ioctl(fd,FS_IOC_GETFLAGS,b'\x00'*4)
        except OSError: refuse('linux_flags_query_failed')
        flags=struct.unpack('I',raw[:4])[0]
        if flags & ~LINUX_ALLOWED_FLAGS: refuse('unsupported_linux_flags')
        return flags
    if SYSTEM=='Darwin':
        st=os.fstat(fd)
        if not hasattr(st,'st_flags'): refuse('unsupported_flag_query')
        if st.st_flags: refuse('unsupported_macos_flags')
        return 0
    refuse('unsupported_platform')

class Meta:
    __slots__=('mode','uid','gid','xattrs','flags','acl')
    def __init__(self,mode,uid,gid,xattrs,flags,acl):
        self.mode,self.uid,self.gid=mode,uid,gid
        self.xattrs,self.flags,self.acl=tuple(xattrs),flags,acl

def meta_to_record(meta):
    return {'mode':meta.mode,'uid':meta.uid,'gid':meta.gid,'flags':meta.flags,'acl':meta.acl,
            'xattrs':[{'name':b64e(n),'value':b64e(v)} for n,v in meta.xattrs]}

def meta_from_record(obj):
    if not isinstance(obj,dict) or set(obj)!={'mode','uid','gid','flags','acl','xattrs'}: refuse('invalid_metadata_record')
    if any(type(obj[k]) is not int or not 0<=obj[k]<=0xffffffff for k in ('mode','uid','gid','flags')): refuse('invalid_metadata_record')
    if obj['mode']>0o777 or obj['acl']!='absent' or not isinstance(obj['xattrs'],list) or len(obj['xattrs'])>MAX_XATTRS: refuse('invalid_metadata_record')
    attrs=[]; total=0
    for item in obj['xattrs']:
        if not isinstance(item,dict) or set(item)!={'name','value'}: refuse('invalid_metadata_record')
        name,value=b64d(item['name']),b64d(item['value'])
        if not name or b'\0' in name: refuse('invalid_metadata_record')
        total+=len(name)+len(value)
        if total>MAX_XATTR_BYTES: refuse('xattr_bounds')
        attrs.append((name,value))
    if attrs!=sorted(attrs) or len({n for n,_ in attrs})!=len(attrs): refuse('invalid_metadata_record')
    return Meta(obj['mode'],obj['uid'],obj['gid'],attrs,obj['flags'],obj['acl'])

def meta_digest(meta): return sha256(canonical(meta_to_record(meta)).encode('utf-8'))

def snapshot_meta(fd):
    before=os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: refuse('not_regular_single_link')
    mode=stat.S_IMODE(before.st_mode)
    if mode & 0o7000: refuse('special_mode_bits')
    flags,acl=read_flags(fd),read_acl(fd); attrs=read_xattrs(fd)
    if stat_signature(before)!=stat_signature(os.fstat(fd)): refuse('changed_while_read')
    return Meta(mode,before.st_uid,before.st_gid,attrs,flags,acl)

def verify_meta(fd,meta):
    if meta_to_record(snapshot_meta(fd))!=meta_to_record(meta): refuse('metadata_mismatch')

def apply_meta(fd,meta):
    st=os.fstat(fd)
    if (st.st_uid,st.st_gid)!=(meta.uid,meta.gid):
        try: os.fchown(fd,meta.uid,meta.gid)
        except OSError: refuse('ownership_preservation_failed')
    os.fchmod(fd,meta.mode); read_acl(fd); write_xattrs(fd,meta.xattrs); verify_meta(fd,meta)
