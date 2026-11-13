#!/usr/bin/env python3
"""AAF -> REAPER project converter (no external dependencies).

Usage:  python3 aaf2rpp.py <file.aaf> [more.aaf ...] [-o OUTPUT_DIR]

For every AAF it creates a folder "<name> - Reaper" next to the AAF
(or inside OUTPUT_DIR) containing "<name>.RPP" and a "SoundFiles"
folder with the audio converted to WAV.
"""
import os
import sys
import struct
import uuid
import re
import shutil
from urllib.parse import unquote, urlparse

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aafreader import AAFFile, AAFObject, WeakRef  # noqa: E402

VERSION = '1.0'

# Operation definition AUIDs
OP_MONO_AUDIO_GAIN = uuid.UUID('9d2ea894-0968-11d3-8a38-0050040ef7d2')
OP_STEREO_AUDIO_GAIN = uuid.UUID('9d2ea893-0968-11d3-8a38-0050040ef7d2')
OP_CHANNEL_COMBINER = uuid.UUID('6b46dd7a-132d-4856-ab21-8b751d8462ec')
# Parameter definition AUIDs
PARAM_AMPLITUDE = uuid.UUID('e4962321-2267-11d3-8a4c-0050040ef7d2')
PARAM_LEVEL = uuid.UUID('e4962320-2267-11d3-8a4c-0050040ef7d2')
# Data definitions
DD_SOUND = {uuid.UUID('01030202-0200-0000-060e-2b3404010101'),
            uuid.UUID('78e1ebe1-152a-11d2-a2ce-00a0c9a0b9bb')}

FILE_DESCRIPTORS = ('PCMDescriptor', 'WAVEDescriptor', 'AIFCDescriptor',
                    'SoundDescriptor', 'FileDescriptor')


class ConvertError(Exception):
    pass


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def u16s(b):
    if b is None:
        return ''
    return b.decode('utf-16-le', 'replace').split('\x00')[0]


def i32(b):
    return struct.unpack('<i', b[:4])[0] if b else 0


def u32(b):
    return struct.unpack('<I', b[:4])[0] if b else 0


def i64(b):
    return struct.unpack('<q', b[:8])[0] if b else 0


def rational(b):
    if not b or len(b) < 8:
        return None
    n, d = struct.unpack('<ii', b[:8])
    return (n, d)


def rat_float(r, default=None):
    if r is None or r[1] == 0:
        return default
    return float(r[0]) / float(r[1])


def indirect_value(b):
    """Decode an AAF indirect value (byte order, type id, payload)."""
    if not b or len(b) < 17:
        return None
    payload = b[17:]
    tid = uuid.UUID(bytes_le=bytes(b[1:17]))
    s = str(tid)
    # Rational
    if s.startswith('03010100-0000-0000-060e-2b3401040101') or len(payload) == 8:
        r = rational(payload)
        if r is not None and s.startswith('03010100'):
            return ('rational', r)
    if len(payload) == 8:
        return ('rational', rational(payload))
    if len(payload) == 4:
        return ('int', i32(payload))
    if len(payload) == 2:
        return ('int', struct.unpack('<h', payload)[0])
    return ('raw', payload)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', s).strip().strip('.')
    return s or 'audio'


def new_guid():
    return '{' + str(uuid.uuid4()).upper() + '}'


def fmt(x):
    s = '%.14f' % x
    s = s.rstrip('0').rstrip('.')
    return s if s not in ('', '-0') else '0'


# ----------------------------------------------------------------------------
# media handling (MXF / WAV / embedded)
# ----------------------------------------------------------------------------
def _ber(f):
    b = f.read(1)
    if not b:
        return None
    b = b[0]
    if b & 0x80:
        n = b & 0x7F
        return int.from_bytes(f.read(n), 'big')
    return b


ESSENCE_PREFIX = bytes.fromhex('060e2b34010201010d010301')


def mxf_essence(path):
    """Return (offset, length, element_type, key) of the sound essence in an MXF."""
    parts = []
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        pos = 0
        while pos < size - 17:
            f.seek(pos)
            key = f.read(16)
            if len(key) < 16:
                break
            ln = _ber(f)
            if ln is None:
                break
            vstart = f.tell()
            if key[:12] == ESSENCE_PREFIX and key[12] in (0x16, 0x06):
                parts.append((vstart, ln, key[14], key))
            pos = vstart + ln
    if not parts:
        raise ConvertError('No audio essence found in %s' % os.path.basename(path))
    return parts


class Media(object):
    """Describes one channel of source audio (a file-mob)."""

    def __init__(self, name, path=None, kind=None, rate=48000, bits=24, channels=1,
                 length=None, embedded=None):
        self.name = name
        self.path = path
        self.kind = kind          # 'mxf', 'wav', 'raw'
        self.rate = rate
        self.bits = bits
        self.channels = channels
        self.length = length      # samples
        self.embedded = embedded  # bytes (raw/wav) for embedded essence
        self.wav = None           # header info for linked WAV files
        self.chan_index = 0       # which channel of a multichannel file this is

    def bps(self):
        return (self.bits + 7) // 8

    def pcm_reader(self):
        """Yield raw little-endian PCM chunks (interleaved if channels>1)."""
        if self.kind == 'mxf':
            parts = mxf_essence(self.path)
            with open(self.path, 'rb') as f:
                for off, ln, etype, key in parts:
                    nsamp = self.length or 0
                    aes3 = (etype in (0x03, 0x04) and self.bits > 16 and
                            nsamp and ln >= nsamp * 4 * self.channels and
                            ln != nsamp * self.bps() * self.channels)
                    f.seek(off)
                    remaining = ln
                    while remaining > 0:
                        n = min(remaining, 1 << 22)
                        n -= n % (4 if aes3 else self.bps() * self.channels)
                        if n <= 0:
                            n = remaining
                        chunk = f.read(n)
                        remaining -= n
                        if aes3:
                            chunk = _aes3_to_pcm24(chunk)
                        yield chunk
        elif self.kind == 'wav':
            info = self.wav
            fnch = info['channels']
            frame = fnch * self.bps()
            with open(self.path, 'rb') as f:
                f.seek(info['data_off'])
                remaining = info['data_len']
                while remaining > 0:
                    n = min(remaining, frame * 262144)
                    chunk = f.read(n)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if fnch > 1 and self.channels == 1:
                        chunk = extract_channel(chunk, fnch, min(self.chan_index, fnch - 1), self.bps())
                    yield chunk
        elif self.kind == 'raw':
            yield self.embedded


def _aes3_to_pcm24(chunk):
    out = bytearray()
    for (w,) in struct.iter_unpack('<I', chunk[:len(chunk) - len(chunk) % 4]):
        out += ((w >> 4) & 0xFFFFFF).to_bytes(3, 'little')
    return bytes(out)


def write_wav(path, rate, bits, channels, chunks_iter):
    bps = (bits + 7) // 8
    with open(path, 'wb') as f:
        f.write(b'RIFF\x00\x00\x00\x00WAVE')
        fmt_chunk = struct.pack('<HHIIHH', 1, channels, rate, rate * bps * channels,
                                bps * channels, bits)
        f.write(b'fmt ' + struct.pack('<I', len(fmt_chunk)) + fmt_chunk)
        f.write(b'data\x00\x00\x00\x00')
        total = 0
        for c in chunks_iter:
            f.write(c)
            total += len(c)
        if total % 2:
            f.write(b'\x00')
        if total > 0xFFFFFFFF - 64:
            raise ConvertError('Audio file larger than 4 GB is not supported: %s' % path)
        f.seek(4)
        f.write(struct.pack('<I', 4 + 8 + len(fmt_chunk) + 8 + total + (total % 2)))
        f.seek(12 + 8 + len(fmt_chunk) + 4)
        f.write(struct.pack('<I', total))
    return total


def wav_info(path):
    """Return dict(channels, rate, bits, data_off, data_len) for a RIFF/RF64 WAV."""
    with open(path, 'rb') as f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] not in (b'RIFF', b'RF64', b'BW64') or hdr[8:12] != b'WAVE':
            return None
        info = {}
        ds64_len = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, ln = ch[:4], struct.unpack('<I', ch[4:])[0]
            pos = f.tell()
            if cid == b'ds64':
                d = f.read(min(ln, 28))
                if len(d) >= 24:
                    ds64_len = struct.unpack('<Q', d[16:24])[0]
            elif cid == b'fmt ':
                d = f.read(min(ln, 40))
                fmt_tag, nch, rate = struct.unpack('<HHI', d[:8])
                bits = struct.unpack('<H', d[14:16])[0]
                info.update(format=fmt_tag, channels=nch, rate=rate, bits=bits)
            elif cid == b'data':
                if ln == 0xFFFFFFFF and ds64_len is not None:
                    ln = ds64_len
                info.update(data_off=pos, data_len=ln)
                break
            f.seek(pos + ln + (ln & 1))
        if 'channels' not in info or 'data_off' not in info:
            return None
        return info


def extract_channel(chunk, nch, idx, bps):
    """Take channel idx out of interleaved PCM."""
    if nch == 1:
        return chunk
    frame = nch * bps
    n = len(chunk) - len(chunk) % frame
    out = bytearray((n // frame) * bps)
    for b in range(bps):
        out[b::bps] = chunk[idx * bps + b:n:frame]
    return bytes(out)


def interleave(chunks_list, bps):
    """Interleave equal-length mono byte strings."""
    n = len(chunks_list)
    if n == 1:
        return chunks_list[0]
    length = min(len(c) for c in chunks_list)
    length -= length % bps
    out = bytearray(length * n)
    stride = bps * n
    for ch, c in enumerate(chunks_list):
        for b in range(bps):
            out[ch * bps + b::stride] = c[b:length:bps]
    return bytes(out)


def _rechunk(gen, size):
    buf = bytearray()
    for c in gen:
        buf += c
        while len(buf) >= size:
            yield bytes(buf[:size])
            del buf[:size]
    if buf:
        yield bytes(buf)


def combined_chunks(medias, skip_samples):
    bps = medias[0].bps()
    chunk = bps * 65536 * 4
    gens = []
    for m, skip in zip(medias, skip_samples):
        g = _rechunk(m.pcm_reader(), chunk)
        gens.append(_skip(g, skip * bps))
    while True:
        parts = []
        for g in gens:
            try:
                parts.append(next(g))
            except StopIteration:
                parts.append(b'')
        if not any(parts):
            break
        m = max(len(p) for p in parts)
        parts = [p + b'\x00' * (m - len(p)) for p in parts]
        yield interleave(parts, bps)


def _skip(gen, nbytes):
    for c in gen:
        if nbytes >= len(c):
            nbytes -= len(c)
            continue
        if nbytes:
            c = c[nbytes:]
            nbytes = 0
        yield c


# ----------------------------------------------------------------------------
# AAF timeline model
# ----------------------------------------------------------------------------
class Item(object):
    def __init__(self):
        self.position = 0.0
        self.length = 0.0
        self.sources = []        # list of (Media, offset_seconds)
        self.gain = 1.0
        self.gain_env = None     # list of (time_sec, gain) or None
        self.fade_in = 0.0
        self.fade_out = 0.0
        self.name = ''


class Converter(object):
    def __init__(self, aaf_path, log=print):
        self.path = aaf_path
        self.log = log
        self.warnings = []
        self.f = AAFFile(aaf_path)
        content = self.f.header.get('Content')
        self.mobs = {}
        for m in content.get('Mobs') or []:
            mid = m.data('MobID')
            if mid:
                self.mobs[bytes(mid)] = m
        self.essence_data = {}
        for ed in content.get('EssenceData') or []:
            mid = ed.data('MobID')
            if mid:
                self.essence_data[bytes(mid)] = ed
        self.media_cache = {}
        self.path_users = {}

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    # -- mob helpers -------------------------------------------------------
    def top_composition(self):
        comps = [m for m in self.mobs.values() if m.isa('CompositionMob')]
        if not comps:
            raise ConvertError('No timeline (composition) found in AAF')
        referenced = set()
        for m in comps:
            for s in m.get('Slots') or []:
                self._collect_refs(s.get('Segment'), referenced)
        top = [m for m in comps if bytes(m.data('MobID')) not in referenced]
        for m in top or comps:
            uc = m.data('UsageCode')
            if uc and uuid.UUID(bytes_le=bytes(uc)) == uuid.UUID('0d010101-0101-0007-060e-2b3404010101'):
                pass
        cand = top or comps
        # prefer one marked TopLevel
        for m in cand:
            uc = m.data('UsageCode')
            if uc and bytes(uc).hex().startswith('0201010d01010007'):
                return m
        return cand[0]

    def _collect_refs(self, seg, out, depth=0):
        if seg is None or depth > 50:
            return
        if seg.isa('SourceClip'):
            sid = seg.data('SourceID')
            if sid:
                out.add(bytes(sid))
        for key in ('Components', 'InputSegments'):
            for c in seg.get(key) or []:
                self._collect_refs(c, out, depth + 1)
        og = seg.get('OperationGroup') if seg.isa('Transition') else None
        if og is not None:
            self._collect_refs(og, out, depth + 1)

    @staticmethod
    def slot_rate(slot):
        r = rational(slot.data('EditRate'))
        return rat_float(r, 25.0)

    @staticmethod
    def find_slot(mob, slot_id):
        for s in mob.get('Slots') or []:
            if u32(s.data('SlotID')) == slot_id:
                return s
        return None

    def is_sound(self, comp):
        dd = comp.get('DataDefinition')
        if isinstance(dd, WeakRef) and dd.auid is not None:
            return dd.auid in DD_SOUND
        return True

    # -- source resolution ---------------------------------------------------
    def resolve_source(self, mob_id, slot_id, t, depth=0):
        """Follow a SourceClip reference down to file media.

        t = time in seconds inside the referenced slot. Returns list of
        (Media, offset_seconds) - one per channel.
        """
        if depth > 20:
            return []
        if not any(mob_id):
            return []
        mob = self.mobs.get(bytes(mob_id))
        if mob is None:
            self.warn('Missing media reference (mob not in AAF)')
            return []
        desc = mob.get('EssenceDescription') if mob.isa('SourceMob') else None
        slot = self.find_slot(mob, slot_id)
        if desc is not None and desc.classname in FILE_DESCRIPTORS or (
                desc is not None and desc.isa('FileDescriptor')):
            media = self.media_for(mob, desc)
            if media is None:
                return []
            origin = 0.0
            if slot is not None:
                origin = i64(slot.data('Origin')) / self.slot_rate(slot)
            return [(media, t + origin)]
        if slot is None:
            return []
        seg = slot.get('Segment')
        return self.resolve_in_segment(seg, t, self.slot_rate(slot), depth)

    def resolve_in_segment(self, seg, t, rate, depth):
        if seg is None:
            return []
        if seg.isa('SourceClip'):
            ref_slot_rate = self._ref_rate(seg) or rate
            start = i64(seg.data('StartTime')) / ref_slot_rate
            return self.resolve_source(seg.data('SourceID') or b'\x00',
                                       u32(seg.data('SourceMobSlotID')),
                                       start + t, depth + 1)
        if seg.isa('Sequence'):
            pos = 0.0
            for c in seg.get('Components') or []:
                ln = i64(c.data('Length')) / rate
                if c.isa('Transition'):
                    pos -= ln
                    continue
                if pos <= t < pos + ln or ln == 0 and abs(t - pos) < 1e-9:
                    return self.resolve_in_segment(c, t - pos, rate, depth + 1)
                pos += ln
            return []
        if seg.isa('OperationGroup'):
            out = []
            for c in seg.get('InputSegments') or []:
                out.extend(self.resolve_in_segment(c, t, rate, depth + 1))
                if self.op_id(seg) != OP_CHANNEL_COMBINER:
                    break
            return out
        if seg.isa('EssenceGroup'):
            choices = seg.get('Choices') or []
            if choices:
                return self.resolve_in_segment(choices[0], t, rate, depth + 1)
        return []

    def _ref_rate(self, clip):
        mob = self.mobs.get(bytes(clip.data('SourceID') or b''))
        if mob is None:
            return None
        slot = self.find_slot(mob, u32(clip.data('SourceMobSlotID')))
        return self.slot_rate(slot) if slot is not None else None

    @staticmethod
    def op_id(og):
        op = og.get('Operation')
        if isinstance(op, WeakRef):
            return op.auid
        return None

    # -- media -------------------------------------------------------------
    def media_for(self, mob, desc):
        key = bytes(mob.data('MobID'))
        if key in self.media_cache:
            return self.media_cache[key]
        name = u16s(mob.data('Name')) or 'audio'
        rate = rat_float(rational(desc.data('AudioSamplingRate')) or
                         rational(desc.data('SampleRate')), 48000.0)
        bits = u32(desc.data('QuantizationBits')) or 24
        ch = u32(desc.data('Channels')) or 1
        length = i64(desc.data('Length')) or None
        m = None
        # embedded essence
        if key in self.essence_data:
            m = self._embedded_media(self.essence_data[key], name, int(rate), bits, ch, length)
        if m is None:
            path = self._locate(desc, name)
            if path is None:
                urls = [u16s(l.data('URLString')) for l in (desc.get('Locator') or [])]
                self.warn('Media file not found for clip "%s" (looked for %s, and in the AAF\'s folder)'
                          % (name, ', '.join(unquote(u) for u in urls if u) or 'no path in AAF'))
                self.media_cache[key] = None
                return None
            ext = os.path.splitext(path)[1].lower()
            kind = 'mxf' if ext == '.mxf' else 'file'
            m = Media(name, path, kind, int(round(rate)), bits, ch, length)
            m.aaf_rate = int(round(rate))
            if ext in ('.wav', '.wave', '.bwf'):
                info = wav_info(path)  # may raise PermissionError (macOS privacy)
                if info and info.get('format') in (1, 0xFFFE):
                    m.kind = 'wav'
                    m.wav = info
                    m.rate = info['rate']
                    m.bits = info['bits']
                    # a linked multichannel file is referenced once per channel
                    m.channels = 1 if info['channels'] > 1 else info['channels']
                    m.file_channels = info['channels']
            users = self.path_users.setdefault(path, [])
            m.chan_index = len(users)
            users.append(key)
        self.media_cache[key] = m
        return m

    def _embedded_media(self, ed, name, rate, bits, ch, length):
        r = ed.raw('Data')
        if r is None:
            return None
        sname = u16s(r[1])
        cfb = self.f.cfb
        chn = cfb.children(ed.dir_idx)
        if sname not in chn:
            return None
        data = cfb.read_stream(chn[sname])
        if data[:4] in (b'RIFF', b'FORM'):
            m = Media(name, None, 'embedded-file', rate, bits, ch, length)
            m.embedded = data
            m.ext = '.wav' if data[:4] == b'RIFF' else '.aif'
            return m
        return Media(name, None, 'raw', rate, bits, ch, length, embedded=data)

    def _locate(self, desc, name):
        cands = []
        for loc in desc.get('Locator') or []:
            url = u16s(loc.data('URLString') or loc.data('Path'))
            if not url:
                continue
            if url.lower().startswith('file:'):
                p = unquote(urlparse(url).path)
                # file://hostname/Volumes/... style
                cands.append(p)
                if re.match(r'^/[A-Za-z]:/', p):
                    cands.append(p[1:])
            else:
                cands.append(url)
        for c in cands:
            if os.path.isfile(c):
                return c
        # search next to the AAF by file name
        here = os.path.dirname(os.path.abspath(self.path))
        names = set(os.path.basename(c.replace('\\', '/')) for c in cands)
        try:
            os.listdir(here)
        except OSError as e:
            self.warn('No permission to read the folder "%s" (%s). On a Mac, allow it in '
                      'System Settings > Privacy & Security > Files and Folders.' % (here, e))
            return None
        for root, _dirs, files in os.walk(here):
            for fn in files:
                if fn in names:
                    return os.path.join(root, fn)
            if root.count(os.sep) - here.count(os.sep) > 3:
                break
        return None

    # -- timeline -------------------------------------------------------------
    def parse_track(self, slot):
        rate = self.slot_rate(slot)
        seg = slot.get('Segment')
        comps = seg.get('Components') if seg is not None and seg.isa('Sequence') else [seg]
        items = []
        pos = 0  # edit units
        prev_item = None
        pending_fade_in = 0
        for c in comps or []:
            if c is None:
                continue
            ln = i64(c.data('Length'))
            if c.isa('Transition'):
                pos -= ln
                if prev_item is not None and prev_item.position + prev_item.length >= (pos + ln) / rate - 1e-9:
                    prev_item.fade_out = ln / rate
                pending_fade_in = ln
                continue
            if c.isa('Filler'):
                pos += ln
                prev_item = None
                pending_fade_in = 0
                continue
            item = self.make_item(c, pos / rate, ln / rate, rate)
            if item is not None:
                if pending_fade_in:
                    item.fade_in = pending_fade_in / rate
                items.append(item)
                prev_item = item
            else:
                prev_item = None
            pending_fade_in = 0
            pos += ln
        return items

    def make_item(self, seg, position, length, rate):
        item = Item()
        item.position = position
        item.length = length
        gain, env, inner = self.unwrap_effects(seg, length)
        item.gain = gain
        item.gain_env = env
        sources = []
        names = []
        for s in inner:
            if s.isa('SourceClip'):
                rr = self._ref_rate(s) or rate
                start = i64(s.data('StartTime')) / rr
                res = self.resolve_source(s.data('SourceID') or b'\x00',
                                          u32(s.data('SourceMobSlotID')), start)
                sources.extend(res)
                mob = self.mobs.get(bytes(s.data('SourceID') or b''))
                if mob is not None:
                    names.append(u16s(mob.data('Name')))
            elif s.isa('Sequence') or s.isa('OperationGroup'):
                sources.extend(self.resolve_in_segment(s, 0.0, rate, 0))
        if not sources:
            return None
        item.sources = sources
        item.name = names[0] if names else sources[0][0].name
        return item

    def unwrap_effects(self, seg, length):
        """Peel gain effects; return (gain, envelope, [source segments])."""
        gain = 1.0
        env = None
        cur = [seg]
        guard = 0
        while guard < 20:
            guard += 1
            nxt = []
            changed = False
            for s in cur:
                if s.isa('OperationGroup'):
                    op = self.op_id(s)
                    inputs = s.get('InputSegments') or []
                    if op in (OP_MONO_AUDIO_GAIN, OP_STEREO_AUDIO_GAIN):
                        g, e = self.read_gain(s, length)
                        if g is not None:
                            gain *= g
                        if e is not None:
                            env = e
                        nxt.extend(inputs[:1])
                        changed = True
                    elif op == OP_CHANNEL_COMBINER:
                        nxt.extend(inputs)
                        changed = True
                    else:
                        self.warn('Unsupported effect ignored (%s)' % op)
                        nxt.extend(inputs[:1])
                        changed = True
                else:
                    nxt.append(s)
            cur = nxt
            if not changed:
                break
        return gain, env, cur

    def read_gain(self, og, length):
        for p in og.get('Parameters') or []:
            d = p.data('Definition')
            pdef = uuid.UUID(bytes_le=bytes(d)) if d and len(d) == 16 else None
            if pdef is not None and pdef != PARAM_AMPLITUDE:
                continue
            if p.isa('ConstantValue'):
                return self.gain_value(p.data('Value')), None
            if p.isa('VaryingValue'):
                pts = []
                for cp in p.get('PointList') or []:
                    tt = rat_float(rational(cp.data('Time')), 0.0)
                    v = self.gain_value(cp.data('Value'))
                    if v is not None:
                        pts.append((tt * length, v))
                return None, pts or None
        return None, None

    def gain_value(self, raw):
        v = indirect_value(raw)
        if v is None:
            return None
        if v[0] == 'rational':
            g = rat_float(v[1])
        elif v[0] == 'int':
            g = float(v[1])
        else:
            return None
        if g is None or g < 0.0 or g > 16.0:
            self.warn('A clip gain value could not be read (%s) and was left at 0 dB'
                      % (bytes(raw[17:]).hex() if raw else '?'))
            return None
        return g

    # -- output -------------------------------------------------------------
    def convert(self, out_root=None, template=None):
        comp = self.top_composition()
        title = u16s(comp.data('Name')) or os.path.splitext(os.path.basename(self.path))[0]
        stem = os.path.splitext(os.path.basename(self.path))[0]
        out_root = out_root or os.path.dirname(os.path.abspath(self.path))
        proj_dir = os.path.join(out_root, safe_name(stem) + ' - Reaper')
        sound_dir = os.path.join(proj_dir, 'SoundFiles')
        os.makedirs(sound_dir, exist_ok=True)

        tc_start = 0.0
        fps = 25
        tracks = []
        project_rate = None
        for slot in comp.get('Slots') or []:
            seg = slot.get('Segment')
            if seg is None:
                continue
            if seg.isa('Timecode'):
                fps = struct.unpack('<H', seg.data('FPS') or b'\x19\x00')[0] or 25
                tc_start = i64(seg.data('Start')) / float(fps)
                continue
            if not self.is_sound(seg):
                continue
            name = u16s(slot.data('SlotName')) or ('Audio %d' % (len(tracks) + 1))
            items = self.parse_track(slot)
            tracks.append((name, items))
            for it in items:
                for m, _o in it.sources:
                    project_rate = project_rate or getattr(m, 'aaf_rate', None) or m.rate

        project_rate = project_rate or 48000
        written = {}
        for _name, items in tracks:
            for it in items:
                it.file = self.render_media(it, sound_dir, written)

        rpp_path = os.path.join(proj_dir, safe_name(stem) + '.RPP')
        with open(rpp_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(self.rpp_text(tracks, project_rate, tc_start, fps, template))
        n_items = sum(len(i) for _n, i in tracks)
        return dict(rpp=rpp_path, tracks=len(tracks), items=n_items,
                    files=len(written), warnings=self.warnings, title=title)

    def render_media(self, item, sound_dir, written):
        medias = [m for m, _o in item.sources]
        offsets = [o for _m, o in item.sources]
        same_offset = max(offsets) - min(offsets) < 1e-6
        # Linked original files: if every channel comes from the same file,
        # copy that whole file into SoundFiles (keeps all audio before/after
        # the clip) or, for video and other formats, use it where it is.
        paths = set(m.path for m in medias)
        if len(paths) == 1 and medias[0].kind in ('file', 'wav') and same_offset:
            src = medias[0].path
            item.soffs = offsets[0]
            fch = getattr(medias[0], 'file_channels', None)
            if len(medias) == 1 and fch and fch > 1:
                item.chanmode = 3 + min(medias[0].chan_index, 1)  # mono left / right
            ext = os.path.splitext(src)[1].lower()
            if ext not in ('.wav', '.wave', '.bwf', '.aif', '.aiff', '.flac', '.mp3', '.ogg'):
                return src
            key = ('copy', src)
            if key in written:
                return written[key]
            fn = safe_name(os.path.basename(src))
            used = set(os.path.basename(v) for v in written.values())
            b, e = os.path.splitext(fn)
            i = 2
            while fn in used:
                fn = '%s_%d%s' % (b, i, e)
                i += 1
            dest = os.path.join(sound_dir, fn)
            self.log('  copying %s' % fn)
            shutil.copyfile(src, dest)
            written[key] = dest
            return dest
        if any(m.kind == 'file' for m in medias):
            self.warn('Clip "%s": its channels come from different non-WAV files; only the first is used'
                      % item.name)
            item.soffs = offsets[0]
            return medias[0].path
        rate = medias[0].rate
        bits = medias[0].bits
        if any(m.rate != rate or m.bits != bits for m in medias):
            self.warn('Clip "%s" mixes different sample formats; only first channel used' % item.name)
            medias, offsets = medias[:1], offsets[:1]
        if same_offset:
            key = tuple(id(m) for m in medias)
            skip = [0] * len(medias)
            item.soffs = offsets[0]
        else:
            key = tuple(id(m) for m in medias) + tuple(round(o * rate) for o in offsets)
            skip = [int(round((o - min(offsets)) * rate)) for o in offsets]
            item.soffs = min(offsets)
        if key in written:
            return written[key]
        base = safe_name(medias[0].name if len(medias) == 1 else
                         re.sub(r'_A0?\d+$', '', medias[0].name) or item.name)
        fn = base + '.wav'
        i = 2
        used = set(os.path.basename(v) for v in written.values())
        while fn in used:
            fn = '%s_%d.wav' % (base, i)
            i += 1
        path = os.path.join(sound_dir, fn)
        if medias[0].kind == 'embedded-file' and len(medias) == 1:
            path = os.path.splitext(path)[0] + medias[0].ext
            with open(path, 'wb') as f:
                f.write(medias[0].embedded)
        else:
            channels = sum(m.channels for m in medias)
            if len(medias) == 1:
                gen = _skip(medias[0].pcm_reader(), skip[0] * medias[0].bps())
            else:
                if any(m.channels != 1 for m in medias):
                    self.warn('Clip "%s": multichannel sources combined as first channels only' % item.name)
                gen = combined_chunks(medias, skip)
            self.log('  writing %s (%d ch)' % (fn, channels))
            write_wav(path, rate, bits, channels, gen)
        written[key] = path
        return path

    def rpp_text(self, tracks, rate, tc_start, fps, template):
        L = []
        if template:
            head = _template_head(template)
            head = re.sub(r'^(\s*SAMPLERATE)\s+\S+', r'\g<1> %d' % rate, head, flags=re.M)
            head = re.sub(r'^(\s*PROJOFFS)\s+\S+', r'\g<1> %s' % fmt(tc_start), head, flags=re.M)
            head = re.sub(r'^(\s*TIMEMODE \S+ \S+ \S+)\s+\S+', r'\g<1> %d' % fps, head, flags=re.M)
            L.append(head.rstrip('\n'))
        else:
            L.append('<REAPER_PROJECT 0.1 "7.0/aaf2rpp" 0')
            L.append('  RIPPLE 0 0')
            L.append('  AUTOXFADE 129')
            L.append('  PROJOFFS %s 0 0' % fmt(tc_start))
            L.append('  TIMEMODE 0 5 -1 %d 0 -1 -1 0' % fps)
            L.append('  SMPTESYNC 0 %d 100 40 1000 300 0 0 1 0 0' % fps)
            L.append('  RECORD_PATH "SoundFiles" ""')
            L.append('  TEMPO 120 4 4 0')
            L.append('  SAMPLERATE %d 1 0' % rate)
        for name, items in tracks:
            nch = 2
            for it in items:
                nch = max(nch, len(it.sources))
            nch += nch % 2
            L.append('  <TRACK %s' % new_guid())
            L.append('    NAME "%s"' % name.replace('"', "'"))
            L.append('    VOLPAN 1 0 -1 -1 1')
            L.append('    MUTESOLO 0 0 0')
            L.append('    NCHAN %d' % nch)
            L.append('    TRACKID %s' % new_guid())
            for it in items:
                L.extend(self.item_lines(it))
            L.append('  >')
        L.append('>')
        return '\n'.join(L) + '\n'

    def item_lines(self, it):
        L = ['    <ITEM']
        L.append('      POSITION %s' % fmt(it.position))
        L.append('      SNAPOFFS 0')
        L.append('      LENGTH %s' % fmt(it.length))
        L.append('      LOOP 0')
        L.append('      ALLTAKES 0')
        L.append('      FADEIN 0 %s 0 1 0 0 0' % fmt(it.fade_in))
        L.append('      FADEOUT 0 %s 0 1 0 0 0' % fmt(it.fade_out))
        L.append('      MUTE 0 0')
        L.append('      SEL 0')
        L.append('      IGUID %s' % new_guid())
        L.append('      NAME "%s"' % it.name.replace('"', "'"))
        L.append('      VOLPAN %s 0 1 -1' % ('%.10f' % it.gain).rstrip('0').rstrip('.'))
        L.append('      SOFFS %s' % fmt(getattr(it, 'soffs', 0.0)))
        L.append('      PLAYRATE 1 1 0 -1 0 0.0025')
        L.append('      CHANMODE %d' % getattr(it, 'chanmode', 0))
        L.append('      GUID %s' % new_guid())
        src = it.file
        ext = os.path.splitext(src)[1].lower()
        stype = {'.wav': 'WAVE', '.aif': 'AIFF', '.aiff': 'AIFF', '.mp3': 'MP3',
                 '.flac': 'FLAC'}.get(ext, 'WAVE')
        if os.path.isabs(src) and '/SoundFiles/' in src.replace('\\', '/'):
            src = os.path.join('SoundFiles', os.path.basename(src))
        L.append('      <SOURCE %s' % stype)
        L.append('        FILE "%s"' % src)
        L.append('      >')
        if it.gain_env:
            L.append('      <VOLENV')
            L.append('        EGUID %s' % new_guid())
            L.append('        ACT 1 -1')
            L.append('        VIS 1 1 1')
            L.append('        LANEHEIGHT 0 0')
            L.append('        ARM 0')
            L.append('        DEFSHAPE 0 -1 -1')
            for t, g in it.gain_env:
                L.append('        PT %s %s 0' % (fmt(t), fmt(g)))
            L.append('      >')
        L.append('    >')
        return L


def _template_head(text):
    """Strip tracks and the closing bracket from a template RPP."""
    lines = text.splitlines()
    out = []
    depth = 0
    skipping = False
    for ln in lines:
        s = ln.strip()
        if skipping:
            if s.startswith('<'):
                depth += 1
            elif s == '>':
                depth -= 1
                if depth == 0:
                    skipping = False
            continue
        if s.startswith('<TRACK'):
            skipping = True
            depth = 1
            continue
        out.append(ln)
    while out and out[-1].strip() in ('>', ''):
        out.pop()
    return '\n'.join(out)


def find_template():
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, 'Template.RPP'),
             os.path.expanduser('~/Library/Application Support/AAF to Reaper/Template.RPP')]
    if os.environ.get('APPDATA'):
        cands.append(os.path.join(os.environ['APPDATA'], 'AAF to Reaper', 'Template.RPP'))
    for cand in cands:
        if os.path.isfile(cand):
            with open(cand, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
    return None


def convert_file(path, out_root=None, log=print):
    conv = Converter(path, log=log)
    return conv.convert(out_root, find_template())


def main(argv):
    out = None
    files = []
    it = iter(argv)
    for a in it:
        if a in ('-o', '--output'):
            out = next(it, None)
        else:
            files.append(a)
    if not files:
        print(__doc__)
        return 2
    rc = 0
    for p in files:
        print('Converting %s' % p)
        try:
            r = convert_file(p, out)
            print('  -> %s  (%d tracks, %d clips, %d audio files)'
                  % (r['rpp'], r['tracks'], r['items'], r['files']))
            for w in r['warnings']:
                print('  warning: ' + w)
        except Exception as e:  # noqa
            rc = 1
            print('  ERROR: %s' % e)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
