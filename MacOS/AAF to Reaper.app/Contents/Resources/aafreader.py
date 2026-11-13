"""Minimal, dependency-free AAF (structured storage) reader.

Reads the Microsoft Compound File container and decodes AAF objects
(properties, strong references, sets, vectors) into Python objects.
Property names come from the file's own MetaDictionary when present,
falling back to a built-in table of baseline AAF property IDs.
"""
import struct
import uuid

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF


class CFB(object):
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.data = f.read()
        d = self.data
        if d[:8] != b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1':
            raise ValueError('Not a structured storage (AAF) file')
        major = struct.unpack_from('<H', d, 0x1A)[0]
        self.sector_shift = struct.unpack_from('<H', d, 0x1E)[0]
        self.mini_shift = struct.unpack_from('<H', d, 0x20)[0]
        self.ssize = 1 << self.sector_shift
        self.msize = 1 << self.mini_shift
        num_fat = struct.unpack_from('<I', d, 0x2C)[0]
        self.dir_start = struct.unpack_from('<I', d, 0x30)[0]
        self.mini_cutoff = struct.unpack_from('<I', d, 0x38)[0]
        self.minifat_start = struct.unpack_from('<I', d, 0x3C)[0]
        num_minifat = struct.unpack_from('<I', d, 0x40)[0]
        difat_start = struct.unpack_from('<I', d, 0x44)[0]
        num_difat = struct.unpack_from('<I', d, 0x48)[0]
        difat = list(struct.unpack_from('<109I', d, 0x4C))
        sec = difat_start
        per = self.ssize // 4 - 1
        for _ in range(num_difat):
            if sec in (ENDOFCHAIN, FREESECT):
                break
            vals = struct.unpack_from('<%dI' % (per + 1), d, self._off(sec))
            difat.extend(vals[:per])
            sec = vals[per]
        difat = [s for s in difat if s not in (FREESECT, ENDOFCHAIN)][:num_fat]
        n = self.ssize // 4
        self.fat = []
        for s in difat:
            self.fat.extend(struct.unpack_from('<%dI' % n, d, self._off(s)))
        dirdata = self._chain(self.dir_start)
        self.entries = []
        for i in range(len(dirdata) // 128):
            e = dirdata[i * 128:(i + 1) * 128]
            nlen = struct.unpack_from('<H', e, 64)[0]
            name = e[:max(nlen - 2, 0)].decode('utf-16-le', 'replace')
            typ = e[66]
            left, right, child = struct.unpack_from('<III', e, 68)
            clsid = e[80:96]
            start = struct.unpack_from('<I', e, 116)[0]
            size = struct.unpack_from('<Q', e, 120)[0]
            if major == 3:
                size &= 0xFFFFFFFF
            self.entries.append(dict(name=name, type=typ, left=left, right=right,
                                     child=child, clsid=clsid, start=start, size=size))
        root = self.entries[0]
        self.ministream = self._chain(root['start'])[:root['size']] if root['start'] != ENDOFCHAIN else b''
        self.minifat = []
        if self.minifat_start not in (ENDOFCHAIN, FREESECT):
            mf = self._chain(self.minifat_start)
            self.minifat = list(struct.unpack('<%dI' % (len(mf) // 4), mf))
        self._children_cache = {}

    def _off(self, sec):
        return (sec + 1) << self.sector_shift

    def _chain(self, start):
        out = []
        sec = start
        seen = 0
        while sec not in (ENDOFCHAIN, FREESECT) and seen < len(self.fat) + 1:
            o = self._off(sec)
            out.append(self.data[o:o + self.ssize])
            sec = self.fat[sec]
            seen += 1
        return b''.join(out)

    def read_stream(self, idx):
        e = self.entries[idx]
        if e['size'] < self.mini_cutoff and idx != 0:
            out = []
            sec = e['start']
            while sec not in (ENDOFCHAIN, FREESECT):
                o = sec * self.msize
                out.append(self.ministream[o:o + self.msize])
                sec = self.minifat[sec]
            return b''.join(out)[:e['size']]
        return self._chain(e['start'])[:e['size']]

    def children(self, idx):
        if idx in self._children_cache:
            return self._children_cache[idx]
        res = {}
        stack = [self.entries[idx]['child']]
        while stack:
            i = stack.pop()
            if i == NOSTREAM or i >= len(self.entries):
                continue
            e = self.entries[i]
            res[e['name']] = i
            stack.append(e['left'])
            stack.append(e['right'])
        self._children_cache[idx] = res
        return res


# Stored forms
SF_DATA = 0x82
SF_DATA_STREAM = 0x42
SF_STRONG_REF = 0x22
SF_STRONG_REF_VECTOR = 0x32
SF_STRONG_REF_SET = 0x3A
SF_WEAK_REF = 0x02
SF_WEAK_REF_VECTOR = 0x12
SF_WEAK_REF_SET = 0x1A
SF_UNIQUE_ID = 0x86
SF_OPAQUE_STREAM = 0x40

# Baseline property ids (used before/without the meta dictionary)
BASE_PIDS = {
    0x0001: 'Header', 0x0002: 'MetaDictionary',
    0x0003: 'ClassDefinitions', 0x0004: 'TypeDefinitions',
    0x0005: 'Identification', 0x0006: 'Name', 0x0007: 'Description',
    0x0008: 'ParentClass', 0x0009: 'Properties', 0x000A: 'IsConcrete',
    0x000B: 'Type', 0x000C: 'IsOptional', 0x000D: 'LocalIdentification',
    0x000E: 'IsUniqueIdentifier',
    0x0101: 'ObjClass', 0x0102: 'Generation',
}


def auid_from_bytes(b):
    """AAF AUID stored as a little-endian GUID."""
    return uuid.UUID(bytes_le=bytes(b))


class AAFObject(object):
    __slots__ = ('file', 'dir_idx', 'name', 'clsid', 'props', 'classname', '_cache')

    def __init__(self, f, dir_idx):
        self.file = f
        self.dir_idx = dir_idx
        e = f.cfb.entries[dir_idx]
        self.name = e['name']
        self.clsid = auid_from_bytes(e['clsid'])
        self.classname = f.class_names.get(self.clsid, str(self.clsid))
        self.props = {}
        self._cache = {}
        self._read_props()

    def _read_props(self):
        cfb = self.file.cfb
        ch = cfb.children(self.dir_idx)
        if 'properties' not in ch:
            return
        data = cfb.read_stream(ch['properties'])
        if len(data) < 4:
            return
        count = struct.unpack_from('<H', data, 2)[0]
        hdr = 4
        entries = []
        for i in range(count):
            pid, sf, ln = struct.unpack_from('<HHH', data, hdr + i * 6)
            entries.append((pid, sf, ln))
        off = hdr + count * 6
        for pid, sf, ln in entries:
            self.props[pid] = (sf, data[off:off + ln])
            off += ln

    def pid_for(self, name):
        return self.file.pid_lookup(self.clsid, name)

    def raw(self, name):
        pid = name if isinstance(name, int) else self.file.pid_lookup(self.clsid, name)
        if pid is None or pid not in self.props:
            return None
        return self.props[pid]

    def has(self, name):
        return self.raw(name) is not None

    def get(self, name, default=None):
        key = name
        if key in self._cache:
            return self._cache[key]
        r = self.raw(name)
        if r is None:
            return default
        sf, val = r
        res = self.file.decode(self, sf, val)
        self._cache[key] = res
        return res

    def data(self, name):
        r = self.raw(name)
        return None if r is None else r[1]

    def isa(self, classname):
        return self.file.is_subclass(self.clsid, classname)

    def __repr__(self):
        return '<%s %s>' % (self.classname, self.name)


class WeakRef(object):
    def __init__(self, key_pid, key):
        self.key_pid = key_pid
        self.key = key

    @property
    def auid(self):
        if len(self.key) == 16:
            return auid_from_bytes(self.key)
        return None

    def __repr__(self):
        return '<WeakRef %s>' % (self.auid or self.key.hex())


def _u16(b):
    s = b.decode('utf-16-le', 'replace')
    return s.split('\x00')[0]


class AAFFile(object):
    def __init__(self, path):
        self.path = path
        self.cfb = CFB(path)
        self.class_names = {}
        self.class_parents = {}
        self.prop_ids = {v: k for k, v in BASE_PIDS.items()}
        self.prop_names = dict(BASE_PIDS)
        self.class_props = {}
        self._lookup_cache = {}
        self._load_meta()
        rootch = self.cfb.children(0)
        self.header = AAFObject(self, rootch['Header-2'])

    # -- decoding ---------------------------------------------------------
    def decode(self, obj, sf, val):
        cfb = self.cfb
        if sf == SF_STRONG_REF:
            nm = _u16(val)
            ch = cfb.children(obj.dir_idx)
            return AAFObject(self, ch[nm]) if nm in ch else None
        if sf == SF_STRONG_REF_VECTOR:
            nm = _u16(val)
            ch = cfb.children(obj.dir_idx)
            idx = ch.get(nm + ' index')
            if idx is None:
                return []
            d = cfb.read_stream(idx)
            count = struct.unpack_from('<I', d, 0)[0]
            keys = struct.unpack_from('<%dI' % count, d, 12)
            out = []
            for k in keys:
                sn = '%s{%x}' % (nm, k)
                if sn in ch:
                    out.append(AAFObject(self, ch[sn]))
            return out
        if sf == SF_STRONG_REF_SET:
            nm = _u16(val)
            ch = cfb.children(obj.dir_idx)
            idx = ch.get(nm + ' index')
            if idx is None:
                return []
            d = cfb.read_stream(idx)
            count, _nf, _lf, _kpid, ksize = struct.unpack_from('<IIIHB', d, 0)
            off = 15
            out = []
            for _ in range(count):
                k = struct.unpack_from('<I', d, off)[0]
                off += 8 + ksize
                sn = '%s{%x}' % (nm, k)
                if sn in ch:
                    out.append(AAFObject(self, ch[sn]))
            return out
        if sf == SF_WEAK_REF:
            # weakref index (u16), key pid (u16), key size (u8), key
            if len(val) >= 5:
                _wi, kpid, ks = struct.unpack_from('<HHB', val, 0)
                return WeakRef(kpid, val[5:5 + ks])
            return None
        if sf in (SF_WEAK_REF_VECTOR, SF_WEAK_REF_SET):
            return _u16(val)
        return val  # raw data; caller interprets

    # -- meta dictionary --------------------------------------------------
    def _load_meta(self):
        rootch = self.cfb.children(0)
        md_idx = rootch.get('MetaDictionary-1')
        if md_idx is None:
            return
        md = _MetaObj(self, md_idx)
        for cd in md.strong_list(0x0003):
            ident = cd.data(0x0005)
            name = cd.data(0x0006)
            if ident is None:
                continue
            cid = auid_from_bytes(ident)
            if name is not None:
                self.class_names[cid] = _u16(name)
            parent = cd.data(0x0008)
            if parent is not None and len(parent) >= 21:
                self.class_parents[cid] = auid_from_bytes(parent[5:21])
            cprops = self.class_props.setdefault(cid, {})
            for pd in cd.strong_list(0x0009):
                pname = pd.data(0x0006)
                lid = pd.data(0x000D)
                if pname is None or lid is None:
                    continue
                pid = struct.unpack('<H', lid[:2])[0]
                pn = _u16(pname)
                cprops[pn] = pid
                self.prop_names[pid] = pn
                # keep first mapping for a name, but prefer baseline (<0x8000)
                if pn not in self.prop_ids or pid < 0x8000 <= self.prop_ids[pn]:
                    self.prop_ids[pn] = pid
        self._fill_baseline_classes()

    def _fill_baseline_classes(self):
        for k, v in BASELINE_CLASSES.items():
            self.class_names.setdefault(uuid.UUID(k), v[0])
            if v[1]:
                self.class_parents.setdefault(uuid.UUID(k), uuid.UUID(v[1]))

    def pid_lookup(self, cid, name):
        key = (cid, name)
        if key in self._lookup_cache:
            return self._lookup_cache[key]
        res = None
        c = cid
        seen = 0
        while c is not None and seen < 32:
            cp = self.class_props.get(c)
            if cp and name in cp:
                res = cp[name]
                break
            nxt = self.class_parents.get(c)
            if nxt == c:
                break
            c = nxt
            seen += 1
        if res is None:
            res = self.prop_ids.get(name)
        self._lookup_cache[key] = res
        return res

    def is_subclass(self, cid, name):
        seen = 0
        while cid is not None and seen < 32:
            if self.class_names.get(cid) == name:
                return True
            nxt = self.class_parents.get(cid)
            if nxt == cid:
                break
            cid = nxt
            seen += 1
        return False


class _MetaObj(object):
    """Lightweight reader for meta dictionary objects (static pids)."""

    def __init__(self, f, idx):
        self.f = f
        self.idx = idx
        self.props = {}
        cfb = f.cfb
        ch = cfb.children(idx)
        if 'properties' in ch:
            data = cfb.read_stream(ch['properties'])
            count = struct.unpack_from('<H', data, 2)[0]
            off = 4 + count * 6
            for i in range(count):
                pid, sf, ln = struct.unpack_from('<HHH', data, 4 + i * 6)
                self.props[pid] = (sf, data[off:off + ln])
                off += ln

    def data(self, pid):
        r = self.props.get(pid)
        return None if r is None else r[1]

    def strong_list(self, pid):
        r = self.props.get(pid)
        if r is None:
            return []
        sf, val = r
        nm = _u16(val)
        cfb = self.f.cfb
        ch = cfb.children(self.idx)
        idx = ch.get(nm + ' index')
        if idx is None:
            return []
        d = cfb.read_stream(idx)
        out = []
        if sf == SF_STRONG_REF_SET:
            count, _a, _b, _kp, ks = struct.unpack_from('<IIIHB', d, 0)
            off = 15
            for _ in range(count):
                k = struct.unpack_from('<I', d, off)[0]
                off += 8 + ks
                sn = '%s{%x}' % (nm, k)
                if sn in ch:
                    out.append(_MetaObj(self.f, ch[sn]))
        else:
            count = struct.unpack_from('<I', d, 0)[0]
            for k in struct.unpack_from('<%dI' % count, d, 12):
                sn = '%s{%x}' % (nm, k)
                if sn in ch:
                    out.append(_MetaObj(self.f, ch[sn]))
        return out


# Baseline class AUIDs -> (name, parent) for files whose meta dictionary
# omits baseline classes. AUIDs follow the SMPTE 06.0E.2B.34 label pattern
# (stored as GUID with the label halves swapped).
def _cls(n):
    return '0d010101-0101-%02x00-060e-2b3402060101' % n


BASELINE_CLASSES = {
    _cls(0x01): ('InterchangeObject', None),
    _cls(0x02): ('Component', _cls(0x01)),
    _cls(0x03): ('Segment', _cls(0x02)),
    _cls(0x04): ('EdgeCode', _cls(0x03)),
    _cls(0x05): ('EssenceGroup', _cls(0x03)),
    _cls(0x06): ('Event', _cls(0x03)),
    _cls(0x07): ('GPITrigger', _cls(0x06)),
    _cls(0x08): ('CommentMarker', _cls(0x06)),
    _cls(0x09): ('Filler', _cls(0x03)),
    _cls(0x0A): ('OperationGroup', _cls(0x03)),
    _cls(0x0B): ('NestedScope', _cls(0x03)),
    _cls(0x0C): ('Pulldown', _cls(0x03)),
    _cls(0x0D): ('ScopeReference', _cls(0x03)),
    _cls(0x0E): ('Selector', _cls(0x03)),
    _cls(0x0F): ('Sequence', _cls(0x03)),
    _cls(0x10): ('SourceReference', _cls(0x03)),
    _cls(0x11): ('SourceClip', _cls(0x10)),
    _cls(0x12): ('TextClip', _cls(0x10)),
    _cls(0x13): ('HTMLClip', _cls(0x12)),
    _cls(0x14): ('Timecode', _cls(0x03)),
    _cls(0x15): ('TimecodeStream', _cls(0x03)),
    _cls(0x16): ('TimecodeStream12M', _cls(0x15)),
    _cls(0x17): ('Transition', _cls(0x02)),
    _cls(0x34): ('Mob', _cls(0x01)),
    _cls(0x35): ('CompositionMob', _cls(0x34)),
    _cls(0x36): ('MasterMob', _cls(0x34)),
    _cls(0x37): ('SourceMob', _cls(0x34)),
    _cls(0x38): ('MobSlot', _cls(0x01)),
    _cls(0x39): ('EventMobSlot', _cls(0x38)),
    _cls(0x3A): ('StaticMobSlot', _cls(0x38)),
    _cls(0x3B): ('TimelineMobSlot', _cls(0x38)),
    _cls(0x3C): ('Parameter', _cls(0x01)),
    _cls(0x3D): ('ConstantValue', _cls(0x3C)),
    _cls(0x3E): ('VaryingValue', _cls(0x3C)),
}
