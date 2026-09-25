"""Native metadata/read defenses; Linux-only contracts are explicitly mocked on macOS."""
from contextlib import contextmanager,ExitStack
import ctypes as C
import errno
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import changeset_fs as fs
import changeset_faults as faults
from changeset_core import ChangesetError,MAX_XATTRS,MAX_XATTR_BYTES,canonical,sha256

class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='changeset-fs-',dir='/private/tmp' if os.path.isdir('/private/tmp') else None)
        self.addCleanup(self.temp.cleanup); self.base=Path(self.temp.name)
        self.root=fs.open_root(str(self.base),owner_only=True); self.addCleanup(self.root.close)
        self.fd=os.open('source',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600,dir_fd=self.root.fd)
        self.addCleanup(os.close,self.fd); os.write(self.fd,b'synthetic\n')
    def assert_refusal(self,code,operation):
        with self.assertRaises(ChangesetError) as exc: operation()
        self.assertEqual(exc.exception.code,code)
    def native_support(self):
        try: fs.require_platform_support(self.root)
        except ChangesetError as exc:
            if exc.code=='unsupported_capability': self.skipTest('Native trusted-xattr support unavailable; no successful Linux coverage claimed')
            raise
    @contextmanager
    def mocked_linux_probe(self):
        """Only native Linux xattr/durability calls are synthetic on this host."""
        attrs={}
        def identity(fd):
            st=os.fstat(fd); return st.st_dev,st.st_ino
        def set_attr(fd,name,value): attrs[(identity(fd),name)]=value
        def get_attr(fd,name): return attrs[(identity(fd),name)]
        with ExitStack() as stack:
            stack.enter_context(patch.object(fs,'SYSTEM','Linux'))
            stack.enter_context(patch.object(fs,'_LINUX_PROOF',None))
            for name,fn in [('setxattr',set_attr),('getxattr',get_attr),('listxattr',lambda fd:[]),('removexattr',lambda *args:None)]:
                stack.enter_context(patch.object(fs.os,name,fn,create=True))
            stack.enter_context(patch.object(fs,'durability_sync'))
            stack.enter_context(patch.object(fs,'sync_dir'))
            try: yield
            finally:
                if fs._LINUX_PROOF is not None:
                    os.close(fs._LINUX_PROOF[0]); fs._LINUX_PROOF=None
    def test_descriptor_read_hash_and_size_bound(self):
        self.assertEqual(fs.read_file_at(self.root.fd,'source')[0],b'synthetic\n')
        self.assertEqual(fs.hash_file_at(self.root.fd,'source')[:2],(sha256(b'synthetic\n'),10))
        self.assert_refusal('file_too_large',lambda:fs.read_file_at(self.root.fd,'source',9))
    def test_same_inode_mutation_while_read_refuses(self):
        real_read=os.read; changed=[]
        def mutate(fd,size):
            data=real_read(fd,size)
            if fd==self.fd and data and not changed:
                changed.append(True); os.pwrite(self.fd,b'X',0)
            return data
        with patch.object(fs.os,'read',mutate):
            self.assert_refusal('changed_while_read',lambda:fs.read_fd(self.fd))
    def test_paths_symlinks_hardlinks_and_fifo_refuse(self):
        for path in ('a/../b','a//b','/absolute','a\\b','','a\x00b','/'.join(['x']*33)):
            with self.subTest(path=repr(path)),self.assertRaises(ChangesetError): fs.normalize_rel(path)
        link=self.base/'root-link'; link.symlink_to(self.base,target_is_directory=True)
        with self.assertRaises(OSError): fs.open_root(str(link))
        (self.base/'symlink').symlink_to(self.base/'source')
        with self.assertRaises(ChangesetError): fs.open_regular_at(self.root.fd,'symlink')
        os.link(self.base/'source',self.base/'hardlink')
        self.assert_refusal('not_regular_single_link',lambda:fs.open_regular_at(self.root.fd,'source'))
        (self.base/'hardlink').unlink(); os.mkfifo(self.base/'fifo')
        self.assert_refusal('not_regular_single_link',lambda:fs.open_regular_at(self.root.fd,'fifo'))
    def test_root_exact_mode_and_inode_ancestry(self):
        child=self.base/'child'; child.mkdir(mode=0o700); child_root=fs.open_root(str(child),owner_only=True)
        self.addCleanup(child_root.close); self.assertTrue(fs.roots_overlap(self.root,child_root))
        expected=fs.root_record(child_root); child.chmod(0o750)
        with self.assertRaises(ChangesetError): fs.open_root(str(child),owner_only=True)
        with self.assertRaises(ChangesetError): fs.verify_root(child_root,expected)
    @unittest.skipUnless(fs.SYSTEM=='Darwin','native Darwin descriptor xattrs')
    def test_native_binary_empty_xattrs_and_exact_copy_without_namespace_omission(self):
        self.native_support(); original=dict(fs.read_xattrs(self.fd))
        wanted=dict(original); wanted[b'com.example.changeset.binary']=b'\x00opaque\xff'; wanted[b'com.example.changeset.empty']=b''
        fs.write_xattrs(self.fd,sorted(wanted.items()))
        self.assertTrue(dict(fs.read_xattrs(self.fd))==wanted)
        meta=fs.snapshot_meta(self.fd); other=os.open('destination',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600,dir_fd=self.root.fd)
        self.addCleanup(os.close,other)
        extra=dict(fs.read_xattrs(other)); extra[b'com.example.changeset.extra']=b'delete me'
        fs.write_xattrs(other,sorted(extra.items())); fs.apply_meta(other,meta)
        self.assertTrue(fs.meta_to_record(fs.snapshot_meta(other))==fs.meta_to_record(meta))
        self.assertNotIn(b'com.example.changeset.extra',dict(fs.read_xattrs(other)))
        self.assertTrue(fs.meta_to_record(fs.meta_from_record(fs.meta_to_record(meta)))==fs.meta_to_record(meta))
    @unittest.skipUnless(fs.SYSTEM=='Darwin','native Darwin compression xattr contract')
    def test_native_showcompression_exposes_attrs_and_compressed_source_refuses(self):
        # Apple's XNU bsd/sys/decmpfs.h defines this little-endian header and
        # compression type 1 (uncompressed data stored in the xattr).
        payload=b'synthetic compression contract\n'*3
        compressed=struct.pack('<IIQ',0x636d7066,1,len(payload))+payload
        fd=os.open('compressed',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600,dir_fd=self.root.fd)
        self.addCleanup(os.close,fd)
        setx=fs._native('fsetxattr',[C.c_int,C.c_char_p,C.c_void_p,C.c_size_t,C.c_uint32,C.c_int],C.c_int)
        listing=fs._native('flistxattr',[C.c_int,C.c_void_p,C.c_size_t,C.c_int],C.c_ssize_t)
        getx=fs._native('fgetxattr',[C.c_int,C.c_char_p,C.c_void_p,C.c_size_t,C.c_uint32,C.c_int],C.c_ssize_t)
        chflags=fs._native('fchflags',[C.c_int,C.c_uint],C.c_int)
        neighbour=(fs.read_fd(self.fd)[0],fs.meta_to_record(fs.snapshot_meta(self.fd)))
        self.assertEqual(setx(fd,b'com.apple.decmpfs',C.create_string_buffer(compressed),len(compressed),0,0),0)
        self.assertEqual(chflags(fd,0x20),0)
        try:
            self.assertTrue(os.fstat(fd).st_flags&0x20)
            self.assertEqual(fs.read_fd(fd)[0],payload)
            size=listing(fd,None,0,0); self.assertGreaterEqual(size,0)
            buf=C.create_string_buffer(max(1,size)); self.assertEqual(listing(fd,buf,size,0),size)
            self.assertNotIn(b'com.apple.decmpfs',buf.raw[:size].split(b'\0'))
            C.set_errno(0)
            self.assertEqual(getx(fd,b'com.apple.decmpfs',None,0,0,0),-1)
            self.assertEqual(C.get_errno(),errno.ENOATTR)
            self.assertIn(b'com.apple.decmpfs',fs._mac_names(fd))
            self.assertEqual(fs._mac_get(fd,b'com.apple.decmpfs',len(compressed)),compressed)
            with patch.object(fs,'_mac_names') as names:
                self.assert_refusal('unsupported_macos_flags',lambda:fs.snapshot_meta(fd))
                names.assert_not_called()
            self.assertTrue((fs.read_fd(self.fd)[0],fs.meta_to_record(fs.snapshot_meta(self.fd)))==neighbour)
        finally: self.assertEqual(chflags(fd,0),0)
    def test_metadata_schema_and_xattr_bounds(self):
        obj={'mode':0o600,'uid':os.geteuid(),'gid':os.getegid(),'flags':0,'acl':'absent','xattrs':[]}
        for key,value in [('mode',True),('flags',False),('uid',-1),('mode',0o4600),('acl','unknown')]:
            bad=dict(obj); bad[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ChangesetError): fs.meta_from_record(bad)
        attrs=[{'name':fs.b64e(b'x'+bytes([n])),'value':''} for n in range(1,66)]
        with self.assertRaises(ChangesetError): fs.meta_from_record(dict(obj,xattrs=attrs))
        with self.assertRaises(ChangesetError): fs.meta_from_record(dict(obj,xattrs=[{'name':fs.b64e(b'x'),'value':fs.b64e(b'a'*MAX_XATTR_BYTES)}]))
        with self.assertRaises(ChangesetError): fs.meta_from_record(dict(obj,xattrs=[{'name':fs.b64e(b'x'),'value':''}]*2))
    @unittest.skipUnless(fs.SYSTEM=='Darwin','native Darwin ACL ABI')
    def test_native_no_empty_nonempty_and_invalid_descriptor_acl(self):
        native=fs._native
        self.assertEqual(fs.read_acl(self.fd),'absent')
        init=native('acl_init',[C.c_int],C.c_void_p); free=native('acl_free',[C.c_void_p],C.c_int)
        setfd=native('acl_set_fd',[C.c_int,C.c_void_p],C.c_int)
        empty=init(0); self.assertTrue(empty)
        try:
            # The kernel normalizes attached empty ACLs to absent. This also
            # exercises the actual library's in-memory empty ACL iterator.
            def provide_empty(name,args,result):
                if name=='acl_get_fd': return lambda fd:empty
                if name=='acl_free': return lambda ptr:0
                return native(name,args,result)
            with patch.object(fs,'_native',provide_empty): self.assertEqual(fs.macos_acl_status(self.fd),'absent')
            self.assertEqual(setfd(self.fd,empty),0); self.assertEqual(fs.read_acl(self.fd),'absent')
        finally: free(empty)
        acl=C.c_void_p(init(1)); entry=C.c_void_p(); permset=C.c_void_p()
        principal=C.create_string_buffer(bytes.fromhex('00112233445566778899aabbccddeeff'),16)
        calls=[('acl_create_entry',[C.POINTER(C.c_void_p),C.POINTER(C.c_void_p)],(C.byref(acl),C.byref(entry))),
               ('acl_set_tag_type',[C.c_void_p,C.c_int],(entry,1)),
               ('acl_set_qualifier',[C.c_void_p,C.c_void_p],(entry,principal)),
               ('acl_get_permset',[C.c_void_p,C.POINTER(C.c_void_p)],(entry,C.byref(permset))),
               ('acl_add_perm',[C.c_void_p,C.c_int],(permset,2))]
        try:
            for name,args,values in calls: self.assertEqual(native(name,args,C.c_int)(*values),0)
            self.assertEqual(setfd(self.fd,acl),0)
            self.assert_refusal('unsupported_acl',lambda:fs.read_acl(self.fd))
        finally: free(acl)
        with self.assertRaises(OSError): fs.macos_acl_status(-1)
    @unittest.skipUnless(fs.SYSTEM=='Darwin','Darwin ACL query-error contract')
    def test_acl_query_failure_is_not_no_acl(self):
        native=fs._native
        def failed(name,args,result):
            if name=='acl_get_fd':
                def denied(fd): C.set_errno(errno.EACCES); return None
                return denied
            return native(name,args,result)
        with patch.object(fs,'_native',failed): self.assert_refusal('acl_query_failed',lambda:fs.read_acl(self.fd))
    @unittest.skipUnless(fs.SYSTEM=='Darwin','native F_FULLFSYNC coverage')
    def test_native_fullfsync_file_and_directory_and_failure_refusal(self):
        fs.durability_sync(self.fd); fs.sync_dir(self.root.fd)
        with patch.object(fs.fcntl,'fcntl',side_effect=OSError(errno.ENOTSUP,'synthetic')):
            self.assert_refusal('unsupported_durability',lambda:fs.durability_sync(self.fd))
    @unittest.skipUnless(fs.SYSTEM=='Darwin','native Darwin flags')
    def test_special_file_flags_and_modes_refuse(self):
        fchflags=fs._native('fchflags',[C.c_int,C.c_uint],C.c_int)
        self.assertEqual(fchflags(self.fd,stat.UF_NODUMP),0)
        try: self.assert_refusal('unsupported_macos_flags',lambda:fs.snapshot_meta(self.fd))
        finally: self.assertEqual(fchflags(self.fd,0),0)
        # This host strips setuid/setgid during chmod. The sticky bit is a
        # native, observable special-mode case and must also be refused.
        os.fchmod(self.fd,0o1600); self.assertEqual(stat.S_IMODE(os.fstat(self.fd).st_mode),0o1600)
        self.assert_refusal('special_mode_bits',lambda:fs.snapshot_meta(self.fd))
    def test_linux_ioctl_errors_and_special_flags_fail_closed(self):
        with patch.object(fs,'SYSTEM','Linux'):
            with patch.object(fs.fcntl,'ioctl',side_effect=OSError(errno.ENOTTY,'synthetic')):
                self.assert_refusal('linux_flags_query_failed',lambda:fs.read_flags(self.fd))
            with patch.object(fs.fcntl,'ioctl',return_value=struct.pack('I',0x10)):
                self.assert_refusal('unsupported_linux_flags',lambda:fs.read_flags(self.fd))
            with patch.object(fs.fcntl,'ioctl',return_value=struct.pack('I',fs.LINUX_ALLOWED_FLAGS)):
                self.assertEqual(fs.read_flags(self.fd),fs.LINUX_ALLOWED_FLAGS)
    def test_linux_acl_absence_distinguishes_query_failure(self):
        with patch.object(fs.os,'getxattr',side_effect=OSError(errno.ENODATA,'synthetic'),create=True):
            self.assertEqual(fs.read_linux_acl(self.fd),'absent')
        with patch.object(fs.os,'getxattr',side_effect=OSError(errno.EOPNOTSUPP,'synthetic'),create=True):
            self.assert_refusal('acl_query_failed',lambda:fs.read_linux_acl(self.fd))
        with patch.object(fs.os,'getxattr',return_value=b'nontrivial',create=True):
            self.assert_refusal('unsupported_acl',lambda:fs.read_linux_acl(self.fd))
    def test_linux_fresh_visibility_and_device_scope_are_required(self):
        st=os.fstat(self.fd); proof=(self.fd,(st.st_dev,st.st_ino),b'v'*32); real_fstat=os.fstat
        with patch.object(fs,'_LINUX_PROOF',proof),patch.object(fs.os,'getxattr',return_value=b'v'*32,create=True):
            self.assertTrue(fs.linux_cap_sys_admin(self.fd))
            with patch.object(fs.os,'getxattr',side_effect=OSError(errno.EPERM,'synthetic'),create=True):
                self.assertFalse(fs.linux_cap_sys_admin(self.fd))
            def foreign(fd):
                if fd==987654: return SimpleNamespace(st_dev=st.st_dev+1)
                return real_fstat(fd)
            with patch.object(fs.os,'fstat',foreign): self.assertFalse(fs.linux_cap_sys_admin(987654))
            with patch.object(fs,'SYSTEM','Linux'),patch.object(fs,'linux_cap_sys_admin',return_value=False),patch.object(fs.os,'listxattr',create=True) as listing:
                self.assert_refusal('unsupported_capability',lambda:fs.read_xattrs(self.fd)); listing.assert_not_called()
    def test_linux_visibility_record_rejects_boolean_in_integer_fields(self):
        path=self.base/'linux-visibility.json'; path.write_bytes(b''); path.chmod(0o600)
        st=path.stat(); record={'version':1,'device':st.st_dev,'inode':st.st_ino,'value':fs.b64e(b'v'*32)}
        for field in ('version','device','inode'):
            bad=dict(record); bad[field]=True; path.write_text(canonical(bad))
            with self.subTest(field=field),patch.object(fs,'SYSTEM','Linux'),patch.object(fs.os,'getxattr',return_value=b'v'*32,create=True),patch.object(fs.os,'listxattr',create=True),patch.object(fs.os,'setxattr',create=True),patch.object(fs.os,'removexattr',create=True):
                self.assert_refusal('unsupported_capability',lambda:fs.require_platform_support(self.root,readonly=True))
    def test_linux_probe_precommit_failures_remove_only_owned_temporary(self):
        for phase in ('fchmod','setxattr','durability','rename'):
            with self.subTest(phase=phase),self.mocked_linux_probe():
                owner=fs if phase=='durability' else fs.os
                name='durability_sync' if phase=='durability' else phase
                failure=ChangesetError('unsupported_durability') if phase=='durability' else OSError(errno.EPERM,'synthetic')
                expected='unsupported_durability' if phase=='durability' else 'unsupported_capability'
                before=set(self.base.iterdir())
                with patch.object(owner,name,side_effect=failure):
                    self.assert_refusal(expected,lambda:fs.require_platform_support(self.root))
                self.assertEqual(set(self.base.iterdir()),before)
    def test_linux_repeated_probe_denials_do_not_consume_inventory_bound(self):
        before=set(self.base.iterdir())
        with self.mocked_linux_probe(),patch.object(fs.os,'setxattr',side_effect=OSError(errno.EPERM,'synthetic')):
            for _ in range(70): self.assert_refusal('unsupported_capability',lambda:fs.require_platform_support(self.root))
        self.assertEqual(set(self.base.iterdir()),before)
    def test_linux_committed_probe_survives_sync_failure_and_readonly_retry(self):
        with self.mocked_linux_probe():
            with patch.object(fs,'sync_dir',side_effect=ChangesetError('unsupported_durability')):
                self.assert_refusal('unsupported_durability',lambda:fs.require_platform_support(self.root))
            path=self.base/'linux-visibility.json'; committed=(path.stat().st_ino,path.read_bytes())
            self.assertFalse(list(self.base.glob('.visibility-*')))
            with patch.object(fs.os,'setxattr',side_effect=AssertionError('read-only proof wrote xattrs')):
                fs.require_platform_support(self.root,readonly=True)
                self.assertTrue(fs.linux_cap_sys_admin(self.fd))
            self.assertEqual((path.stat().st_ino,path.read_bytes()),committed)
    def test_linux_probe_cleanup_preserves_foreign_replacement_and_hardlinks(self):
        for mutation in ('foreign','hardlink'):
            with self.subTest(mutation=mutation),self.mocked_linux_probe():
                saved={}
                def interfere(fd,name,value):
                    path=next(self.base.glob('.visibility-*'))
                    retained=self.base/'retained-probe'
                    if mutation=='foreign':
                        path.rename(retained); path.write_bytes(b'foreign replacement'); path.chmod(0o600)
                    else: os.link(path,retained)
                    for p in (path,retained): saved[p]=(p.stat().st_ino,p.stat().st_nlink,p.read_bytes())
                    raise OSError(errno.EPERM,'synthetic')
                with patch.object(fs.os,'setxattr',interfere):
                    self.assert_refusal('unsupported_capability',lambda:fs.require_platform_support(self.root))
                for path,expected in saved.items():
                    self.assertEqual((path.stat().st_ino,path.stat().st_nlink,path.read_bytes()),expected)
                for path in saved: path.unlink()
    def test_linux_probe_unprovable_creation_and_failed_cleanup_preserve_refusal(self):
        with self.mocked_linux_probe(),patch.object(fs.os,'fstat',side_effect=OSError(errno.EIO,'synthetic')):
            self.assert_refusal('unsupported_capability',lambda:fs.require_platform_support(self.root))
        leftovers=list(self.base.glob('.visibility-*')); self.assertEqual(len(leftovers),1)
        leftovers[0].unlink()
        with self.mocked_linux_probe(),patch.object(fs.os,'setxattr',side_effect=OSError(errno.EPERM,'synthetic')),patch.object(fs.os,'unlink',side_effect=OSError(errno.EACCES,'synthetic')):
            self.assert_refusal('unsupported_capability',lambda:fs.require_platform_support(self.root))
        self.assertEqual(len(list(self.base.glob('.visibility-*'))),1)
    def test_atomic_record_fault_preserves_temporary_without_commit(self):
        class Interrupted(Exception): pass
        def hook(point,detail):
            if point=='before_record_commit': raise Interrupted()
        faults.set_hook(hook)
        try:
            with self.assertRaises(Interrupted): fs.write_once_at(self.root.fd,'record.json',b'{"literal":true}')
        finally: faults.clear_hook()
        self.assertFalse((self.base/'record.json').exists())
        temporaries=list(self.base.glob('.tmp-*')); self.assertEqual(len(temporaries),1)
        self.assertEqual(temporaries[0].read_bytes(),b'{"literal":true}')
        fs.write_once_at(self.root.fd,'record.json',b'{"literal":true}')
        self.assertEqual((self.base/'record.json').read_bytes(),b'{"literal":true}')
        with self.assertRaises(ChangesetError): fs.write_once_at(self.root.fd,'record.json',b'{}')
        self.assertEqual((self.base/'record.json').read_bytes(),b'{"literal":true}')

if __name__=='__main__': unittest.main()
