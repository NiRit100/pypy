
""" Storage format:
for each operation (inputargs numbered with negative numbers)
<opnum> [size-if-unknown-arity] [<arg0> <arg1> ...] [descr-or-snapshot-index]

<opnum> is 1 byte
size is 1 byte
the args are varsized
the index is varsized

Snapshot index for guards are an index into the _snapshot_data byte array.
Snapshots reference arrays of (encoded) boxes, the data of which is stored
in _snapshot_array_data.

Top snapshots start with:
    vable_array_index
    vref_array_index 
    ... then continue like a normal snapshot

regular snapshots:
    jitcode_index or -1 for empty snapshots
    pc
    array_index
    prev

prev:
    an index into _snapshot_data
    SNAPSHOT_PREV_NONE for the very final snapshot
    SNAPSHOT_PREV_COMES_NEXT if the prev snapshot comes right *after* the
    current one
"""

from rpython.jit.metainterp.history import (
    ConstInt, Const, ConstFloat, ConstPtr, new_ref_dict, SwitchToBlackhole,
    ConstPtrJitCode)
from rpython.jit.metainterp.resoperation import AbstractResOp, AbstractInputArg,\
    ResOperation, oparity, rop, opwithdescr, GuardResOp, IntOp, FloatOp, RefOp,\
    opclasses
from rpython.rlib.rarithmetic import intmask, r_uint
from rpython.rlib.objectmodel import we_are_translated, specialize, always_inline
from rpython.rlib.jit import Counters
from rpython.rtyper.lltypesystem import rffi, lltype, llmemory
from rpython.rlib.debug import make_sure_not_resized

TAGINT, TAGCONSTPTR, TAGCONSTOTHER, TAGBOX = range(4)
TAGMASK = 0x3
TAGSHIFT = 2

INIT_SIZE = 4096

# XXX todos left:
# - should the snapshots also go somewhere in a more compact form? just right
#   into the byte buffer? or into its own global snapshot buffer?
# - SnapshotIterator is very inefficient
# - don't do snapshot array reordering in resume.capture_resumedata
# - move vable_array and vref_array to the *front* of the top snapshot

def encode_varint_signed(i, res):
    # https://en.wikipedia.org/wiki/LEB128 signed variant
    more = True
    startlen = len(res)
    while more:
        lowest7bits = i & 0b1111111
        i >>= 7
        if ((i == 0) and (lowest7bits & 0b1000000) == 0) or (
            (i == -1) and (lowest7bits & 0b1000000) != 0
        ):
            more = False
        else:
            lowest7bits |= 0b10000000
        res.append(chr(lowest7bits))
    return len(res) - startlen

@always_inline
def decode_varint_signed(b, index=0):
    res = 0
    shift = 0
    while True:
        byte = ord(b[index])
        res = res | ((byte & 0b1111111) << shift)
        index += 1
        shift += 7
        if not (byte & 0b10000000):
            if byte & 0b1000000:
                res |= -1 << shift
            return res, index

def skip_varint_signed(b, index, skip=1):
    while True:
        byte = ord(b[index])
        index += 1
        if not (byte & 0b10000000):
            skip -= 1
            if not skip:
                return index

def varint_only_decode(b, index, skip=0):
    if skip:
        index = skip_varint_signed(b, index, skip)
    res = 0
    shift = 0
    while True:
        byte = ord(b[index])
        res = res | ((byte & 0b1111111) << shift)
        index += 1
        shift += 7
        if not (byte & 0b10000000):
            if byte & 0b1000000:
                res |= -1 << shift
            return res

# chosen such that constant ints need at most 4 bytes
SMALL_INT_STOP  = 0x40000
assert encode_varint_signed(SMALL_INT_STOP, []) <= 4
SMALL_INT_START = -0x40001
assert encode_varint_signed(SMALL_INT_START, []) <= 4

class BaseTrace(object):
    pass

SNAPSHOT_PREV_NEEDS_PATCHING = -3
SNAPSHOT_PREV_NONE = -2
SNAPSHOT_PREV_COMES_NEXT = -1

class TopDownSnapshotIterator(object):
    """ iterates over encoded snapshots, but in top-down order, ie the most
    recent call frame is returned last. more efficient, because that's the
    order than things are encoded in."""

    def __init__(self, trace, snapshot_index):
        self.trace = trace

        self._index = snapshot_index
        self.vable_array_index = self.decode_snapshot_int()
        self.vref_array_index = self.decode_snapshot_int()
        self.snapshot_index = self._index

    def iter_vable_array(self):
        return BoxArrayIter.make(self.vable_array_index, self.trace._snapshot_array_data)

    def iter_vref_array(self):
        return BoxArrayIter.make(self.vref_array_index, self.trace._snapshot_array_data)

    def iter_array(self, snapshot_index):
        array = varint_only_decode(self.trace._snapshot_data, snapshot_index, skip=2)
        return BoxArrayIter.make(array, self.trace._snapshot_array_data)

    def length(self, snapshot_index):
        array = varint_only_decode(self.trace._snapshot_data, snapshot_index, skip=2)
        length, _ = decode_varint_signed(self.trace._snapshot_array_data, array)
        return length

    def prev(self, snapshot_index):
        self._index = skip_varint_signed(self.trace._snapshot_data, snapshot_index, skip=3)
        prev = self.decode_snapshot_int()
        assert prev != SNAPSHOT_PREV_NEEDS_PATCHING
        if prev == SNAPSHOT_PREV_COMES_NEXT:
            prev = self._index
        return prev

    def unpack_jitcode_pc(self, snapshot_index):
        self._index = snapshot_index
        jitcode_index = self.decode_snapshot_int()
        pc = self.decode_snapshot_int()
        return jitcode_index, pc

    def is_empty_snapshot(self, snapshot_index):
        # must be a top snapshot. it's empty if the jitcode index is -1
        return varint_only_decode(self.trace._snapshot_data, snapshot_index, skip=2) == -1

    def decode_snapshot_int(self):
        result, self._index = decode_varint_signed(self.trace._snapshot_data, self._index)
        return result

    def __iter__(self):
        return self

    def next(self):
        res = self.snapshot_index
        if res == SNAPSHOT_PREV_NONE:
            raise StopIteration
        self.snapshot_index = self.prev(res)
        return res


class SnapshotIterator(object):
    def __init__(self, main_iter, snapshot_index):
        self.main_iter = main_iter
        # reverse the snapshots and store the vable, vref lists
        it = TopDownSnapshotIterator(main_iter.trace, snapshot_index)
        self.topdown_snapshot_iter = it
        self.vable_array = it.iter_vable_array()
        self.vref_array = it.iter_vref_array()
        self.size = self.vable_array.total_length + self.vref_array.total_length + 3
        self.framestack = []
        if it.is_empty_snapshot(snapshot_index):
            return
        for snapshot_index in it:
            self.framestack.append(snapshot_index)
            self.size += it.length(snapshot_index) + 2
        self.framestack.reverse()

    def iter_array(self, snapshot_index):
        return self.topdown_snapshot_iter.iter_array(snapshot_index)

    def get(self, index):
        return self.main_iter._untag(index)

    def unpack_jitcode_pc(self, snapshot_index):
        return self.topdown_snapshot_iter.unpack_jitcode_pc(snapshot_index)

    def unpack_array(self, arr):
        # NOT_RPYTHON
        # for tests only
        if isinstance(arr, list):
            arr = BoxArrayIter(arr)
        return [self.get(i) for i in arr]

def _update_liverange(item, index, liveranges):
    tag, v = untag(item)
    if tag == TAGBOX:
        liveranges[v] = index

def update_liveranges(snapshot_index, trace, index, liveranges):
    it = TopDownSnapshotIterator(trace, snapshot_index)
    for item in it.iter_vable_array():
        _update_liverange(item, index, liveranges)
    for item in it.iter_vref_array():
        _update_liverange(item, index, liveranges)
    for snapshot_index in it:
        for item in it.iter_array(snapshot_index):
            _update_liverange(item, index, liveranges)

class TraceIterator(BaseTrace):
    def __init__(self, trace, start, end, force_inputargs=None,
                 metainterp_sd=None):
        self.trace = trace
        self.metainterp_sd = metainterp_sd
        self.all_descr_len = len(metainterp_sd.all_descrs)
        self._cache = [None] * trace._index
        if force_inputargs is not None:
            # the trace here is cut and we're working from
            # inputargs that are in the middle, shuffle stuff around a bit
            self.inputargs = [rop.inputarg_from_tp(arg.type) for
                              arg in force_inputargs]
            for i, arg in enumerate(force_inputargs):
                self._cache[arg.get_position()] = self.inputargs[i]
        else:
            self.inputargs = [rop.inputarg_from_tp(arg.type) for
                              arg in self.trace.inputargs]
            for i, arg in enumerate(self.inputargs):
               self._cache[self.trace.inputargs[i].get_position()] = arg
        self.start = start
        self.pos = start
        self._count = start
        self._index = start
        self.start_index = start
        self.end = end

    def get_dead_ranges(self):
        return self.trace.get_dead_ranges()

    def kill_cache_at(self, pos):
        if pos:
            self._cache[pos] = None

    def replace_last_cached(self, oldbox, box):
        assert self._cache[self._index - 1] is oldbox
        self._cache[self._index - 1] = box

    def _get(self, i):
        res = self._cache[i]
        assert res is not None
        return res

    def done(self):
        return self.pos >= self.end

    def _nextbyte(self):
        if self.done():
            raise IndexError
        res = ord(self.trace._ops[self.pos])
        self.pos += 1
        return res
        
    def _next(self):
        if self.done():
            raise IndexError
        b = self.trace._ops
        index = self.pos
        res = 0
        shift = 0
        while True:
            byte = ord(b[index])
            res = res | ((byte & 0b1111111) << shift)
            index += 1
            shift += 7
            if not (byte & 0b10000000):
                if byte & 0b1000000:
                    res |= -1 << shift
                self.pos = index
                return res

    def _untag(self, tagged):
        tag, v = untag(tagged)
        if tag == TAGBOX:
            return self._get(v)
        elif tag == TAGINT:
            return ConstInt(v + SMALL_INT_START)
        elif tag == TAGCONSTPTR:
            return ConstPtr(self.trace._refs[v])
        elif tag == TAGCONSTOTHER:
            if v & 1:
                return ConstFloat(self.trace._floats[v >> 1])
            else:
                return ConstInt(self.trace._bigints[v >> 1])
        else:
            assert False

    def get_snapshot_iter(self, index):
        return SnapshotIterator(self, index)

    def next_element_update_live_range(self, index, liveranges):
        opnum = self._nextbyte()
        if oparity[opnum] == -1:
            argnum = self._nextbyte()
        else:
            argnum = oparity[opnum]
        for i in range(argnum):
            tagged = self._next()
            tag, v = untag(tagged)
            if tag == TAGBOX:
                liveranges[v] = index
        if opclasses[opnum].type != 'v':
            liveranges[index] = index
        if opwithdescr[opnum]:
            descr_index = self._next()
            if rop.is_guard(opnum):
                update_liveranges(descr_index, self.trace, index,
                                  liveranges)
        if opclasses[opnum].type != 'v':
            return index + 1
        return index

    def next(self):
        opnum = self._nextbyte()
        argnum = oparity[opnum]
        if argnum == -1:
            argnum = self._nextbyte()
        if not (0 <= oparity[opnum] <= 3):
            args = []
            for i in range(argnum):
                args.append(self._untag(self._next()))
            res = ResOperation(opnum, args)
        else:
            cls = opclasses[opnum]
            res = cls()
            argnum = oparity[opnum]
            if argnum == 0:
                pass
            elif argnum == 1:
                res.setarg(0, self._untag(self._next()))
            elif argnum == 2:
                res.setarg(0, self._untag(self._next()))
                res.setarg(1, self._untag(self._next()))
            else:
                assert argnum == 3
                res.setarg(0, self._untag(self._next()))
                res.setarg(1, self._untag(self._next()))
                res.setarg(2, self._untag(self._next()))
        descr_index = -1
        if opwithdescr[opnum]:
            descr_index = self._next()
            if descr_index == 0 or rop.is_guard(opnum):
                descr = None
            else:
                if descr_index < self.all_descr_len + 1:
                    descr = self.metainterp_sd.all_descrs[descr_index - 1]
                else:
                    descr = self.trace._descrs[descr_index - self.all_descr_len - 1]
                res.setdescr(descr)
            if rop.is_guard(opnum): # all guards have descrs
                assert isinstance(res, GuardResOp)
                res.rd_resume_position = descr_index
        if res.type != 'v':
            self._cache[self._index] = res
            self._index += 1
        self._count += 1
        return res

class CutTrace(BaseTrace):
    def __init__(self, trace, start, count, index, inputargs):
        self.trace = trace
        self.start = start
        self.inputargs = inputargs
        self.count = count
        self.index = index

    def cut_at(self, cut):
        assert cut[1] > self.count
        self.trace.cut_at(cut)

    def get_iter(self):
        iter = TraceIterator(self.trace, self.start, self.trace._pos,
                             self.inputargs,
                             metainterp_sd=self.trace.metainterp_sd)
        iter._count = self.count
        iter.start_index = self.index
        iter._index = self.index
        return iter

def combine_uint(index1, index2):
    assert 0 <= index1 < 65536
    assert 0 <= index2 < 65536
    return index1 << 16 | index2 # it's ok to return signed here,
    # we need only 32bit, but 64 is ok for now

def unpack_uint(packed):
    return (packed >> 16) & 0xffff, packed & 0xffff

class BoxArrayIter(object):
    def __init__(self, index, data):
        self.length, self.position = decode_varint_signed(data, index)
        self.total_length = self.length
        self.data = data

    def __repr__(self):
        if self.length == 0 and self.data == '\x00':
            return "BoxArrayIter.BOXARRAYITER0"
        return "<BoxArrayIter length=%s>" % self.length

    @staticmethod
    def make(index, data):
        if data[index] == '\x00':
            return BoxArrayIter.BOXARRAYITER0
        return BoxArrayIter(index, data)

    def __iter__(self):
        return self

    def next(self):
        if self.length == 0:
            raise StopIteration
        self.length -= 1
        item, self.position = decode_varint_signed(self.data, self.position)
        return item

BoxArrayIter.BOXARRAYITER0 = BoxArrayIter(0, ['\x00'])


class Trace(BaseTrace):
    _deadranges = (-1, None)

    def __init__(self, max_num_inputargs, metainterp_sd):
        self.metainterp_sd = metainterp_sd
        self._ops = ['\x00'] * INIT_SIZE
        make_sure_not_resized(self._ops)
        self._pos = 0
        self._consts_bigint = 0
        self._consts_float = 0
        self._total_snapshots = 0
        self._consts_ptr = 0
        self._consts_ptr_nodict = 0
        self._descrs = [None]
        self._refs = [lltype.nullptr(llmemory.GCREF.TO)]
        self._refs_dict = new_ref_dict()
        self._bigints = []
        self._bigints_dict = {}
        self._floats = []
        self._snapshot_data = []
        self._snapshot_array_data = ['\x00']
        if not we_are_translated() and isinstance(max_num_inputargs, list): # old api for tests
            self.inputargs = max_num_inputargs
            for i, box in enumerate(max_num_inputargs):
                box.position_and_flags = r_uint(i << 1)
            max_num_inputargs = len(max_num_inputargs)

        self.max_num_inputargs = max_num_inputargs
        self._count = max_num_inputargs # total count
        self._index = max_num_inputargs # "position" of resulting resops
        self._start = max_num_inputargs
        self._pos = max_num_inputargs
        self.liveranges = [0] * max_num_inputargs

    def set_inputargs(self, inputargs):
        self.inputargs = inputargs
        if not we_are_translated():
            set_positions = {box.get_position() for box in inputargs}
            assert len(set_positions) == len(inputargs)
            assert not set_positions or max(set_positions) < self.max_num_inputargs

    def _double_ops(self):
        self._ops = self._ops + ['\x00'] * len(self._ops)

    def append_byte(self, c):
        assert 0 <= c < 256
        if self._pos >= len(self._ops):
            self._double_ops()
        self._ops[self._pos] = chr(c)
        self._pos += 1

    def append_int(self, i):
        more = True
        while more:
            lowest7bits = i & 0b1111111
            i >>= 7
            if ((i == 0) and (lowest7bits & 0b1000000) == 0) or (
                (i == -1) and (lowest7bits & 0b1000000) != 0
            ):
                more = False
            else:
                lowest7bits |= 0b10000000
            self.append_byte(lowest7bits)

    def tracing_done(self):
        from rpython.rlib.debug import debug_start, debug_stop, debug_print
        self._bigints_dict = {}
        self._refs_dict = new_ref_dict()
        debug_start("jit-trace-done")
        debug_print("trace length:", self._pos)
        debug_print(" total snapshots:", self._total_snapshots)
        debug_print(" snapshot data:", len(self._snapshot_data))
        debug_print(" snapshot array data:", len(self._snapshot_array_data))
        debug_print(" bigint consts: " + str(self._consts_bigint), len(self._bigints))
        debug_print(" float consts: " + str(self._consts_float), len(self._floats))
        debug_print(" ref consts: " + str(self._consts_ptr) + " " + str(self._consts_ptr_nodict),  len(self._refs))
        debug_print(" descrs:", len(self._descrs))
        debug_stop("jit-trace-done")

    def length(self):
        return self._pos

    def cut_point(self):
        return self._pos, self._count, self._index, len(self._snapshot_data), len(self._snapshot_array_data)

    def cut_at(self, end):
        self._pos = end[0]
        self._count = end[1]
        index = end[2]
        assert index >= 0
        self._index = index
        del self.liveranges[index:]

    def cut_trace_from(self, (start, count, index, x, y), inputargs):
        return CutTrace(self, start, count, index, inputargs)

    def _cached_const_int(self, box):
        return v

    def _cached_const_ptr(self, box):
        assert isinstance(box, ConstPtr)
        addr = box.getref_base()
        if not addr:
            return 0
        if isinstance(box, ConstPtrJitCode):
            index = box.opencoder_index
            if index >= 0:
                self._consts_ptr_nodict += 1
                assert self._refs[index] == addr
                return index
        v = self._refs_dict.get(addr, -1)
        if v == -1:
            v = len(self._refs)
            self._refs_dict[addr] = v
            self._refs.append(addr)
        if isinstance(box, ConstPtrJitCode):
            box.opencoder_index = v
        return v

    def _encode(self, box):
        if isinstance(box, Const):
            if (isinstance(box, ConstInt) and
                isinstance(box.getint(), int) and # symbolics
                SMALL_INT_START <= box.getint() < SMALL_INT_STOP):
                return tag(TAGINT, box.getint() - SMALL_INT_START)
            elif isinstance(box, ConstInt):
                self._consts_bigint += 1
                value = box.getint()
                if not isinstance(value, int):
                    # symbolics, for tests, don't worry about caching
                    v = len(self._bigints) << 1
                    self._bigints.append(value)
                else:
                    v = self._bigints_dict.get(value, -1)
                    if v == -1:
                        v = len(self._bigints) << 1
                        self._bigints_dict[value] = v
                        self._bigints.append(value)
                return tag(TAGCONSTOTHER, v)
            elif isinstance(box, ConstFloat):
                # don't intern float constants
                self._consts_float += 1
                v = (len(self._floats) << 1) | 1
                self._floats.append(box.getfloatstorage())
                return tag(TAGCONSTOTHER, v)
            else:
                self._consts_ptr += 1
                v = self._cached_const_ptr(box)
                return tag(TAGCONSTPTR, v)
        elif isinstance(box, AbstractResOp):
            position = box.get_position()
            assert position >= 0
            # every time something is used we assume that it lives to the
            # current _index
            self.liveranges[position] = self._index
            return tag(TAGBOX, position)
        else:
            assert False, "unreachable code"

    def _op_start(self, opnum, num_argboxes):
        old_pos = self._pos
        self.append_byte(opnum)
        expected_arity = oparity[opnum]
        if expected_arity == -1:
            self.append_byte(num_argboxes)
        else:
            assert num_argboxes == expected_arity
        return old_pos

    def _op_end(self, opnum, descr, old_pos):
        if opwithdescr[opnum]:
            if descr is None:
                # just for debugging: add an 's' for missing snapshots, will be
                # patched
                self.append_byte(ord('s'))
            else:
                self.append_int(self._encode_descr(descr))
        self._count += 1
        if opclasses[opnum].type != 'v':
            self._index += 1
            self.liveranges.append(self._index)
            assert len(self.liveranges) == self._index

    def record_op(self, opnum, argboxes, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, len(argboxes))
        for box in argboxes:
            self.append_int(self._encode(box))
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op0(self, opnum, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 0)
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op1(self, opnum, argbox1, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 1)
        self.append_int(self._encode(argbox1))
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op2(self, opnum, argbox1, argbox2, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 2)
        self.append_int(self._encode(argbox1))
        self.append_int(self._encode(argbox2))
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op3(self, opnum, argbox1, argbox2, argbox3, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 3)
        self.append_int(self._encode(argbox1))
        self.append_int(self._encode(argbox2))
        self.append_int(self._encode(argbox3))
        self._op_end(opnum, descr, old_pos)
        return pos

    def _encode_descr(self, descr):
        descr_index = descr.get_descr_index()
        if descr_index != -1:
            return descr_index + 1
        self._descrs.append(descr)
        return len(self._descrs) - 1 + len(self.metainterp_sd.all_descrs) + 1

    # ____________________________________________________________
    # snapshots

    def _list_of_boxes(self, boxes):
        boxes_list_storage = self.new_array(len(boxes))
        for i in range(len(boxes)):
            self._add_box_to_storage(boxes_list_storage, boxes[i])
        return boxes_list_storage

    def _list_of_boxes_virtualizable(self, boxes):
        if not boxes:
            return self.new_array(0)
        boxes_list_storage = self.new_array(len(boxes))
        # the virtualizable is at the end, move it to the front in the snapshot
        self._add_box_to_storage(boxes_list_storage, boxes[-1])
        for i in range(len(boxes) - 1):
            self._add_box_to_storage(boxes_list_storage, boxes[i])
        return boxes_list_storage

    def new_array(self, lgt):
        if lgt == 0:
            return 0
        res = len(self._snapshot_array_data)
        self.append_snapshot_array_data_int(lgt)
        return res

    def _add_box_to_storage(self, boxes_list_storage, box):
        self.append_snapshot_array_data_int(self._encode(box))
        return boxes_list_storage

    def append_snapshot_array_data_int(self, i):
        encode_varint_signed(i, self._snapshot_array_data)

    def append_snapshot_data_int(self, i):
        encode_varint_signed(i, self._snapshot_data)

    def _encode_snapshot(self, index, pc, array, is_last=False):
        res = len(self._snapshot_data)
        self.append_snapshot_data_int(index)
        self.append_snapshot_data_int(pc)
        # arrays are indexes into self._snapshot_data
        self.append_snapshot_data_int(array)
        # for prev. can be SNAPSHOT_PREV_COMES_NEXT, which means the prev
        # snapshot comes right afterwards in snapshots. or it can be
        # SNAPSHOT_PREV_NONE, which means there are no more snapshots. or it's
        # a positive number, which means it's already somewhere in snapshot at
        # some other index
        if is_last:
            self.append_snapshot_data_int(SNAPSHOT_PREV_NONE)
        else:
            self.append_snapshot_data_int(SNAPSHOT_PREV_NEEDS_PATCHING)
        return res

    def create_top_snapshot(self, frame, vable_boxes, vref_boxes, after_residual_call=False, is_last=False):
        self._total_snapshots += 1
        array = frame.get_list_of_active_boxes(False, self.new_array, self._add_box_to_storage,
                after_residual_call=after_residual_call)
        s = len(self._snapshot_data)
        vable_array = self._list_of_boxes_virtualizable(vable_boxes)
        vref_array = self._list_of_boxes(vref_boxes)
        self.append_snapshot_data_int(vable_array)
        self.append_snapshot_data_int(vref_array)
        self._encode_snapshot(
                frame.jitcode.index,
                frame.pc,
                array,
                is_last=is_last)
        assert self._ops[self._pos - 1] == 's'
        self._pos -= 1
        self.append_int(s)
        return s

    def create_empty_top_snapshot(self, vable_boxes, vref_boxes):
        self._total_snapshots += 1
        s = len(self._snapshot_data)
        array = self._list_of_boxes([])
        vable_array = self._list_of_boxes_virtualizable(vable_boxes)
        vref_array = self._list_of_boxes(vref_boxes)
        self.append_snapshot_data_int(vable_array)
        self.append_snapshot_data_int(vref_array)
        self._encode_snapshot(
                -1,
                0,
                array,
                is_last=True)
        assert self._ops[self._pos - 1] == 's'
        self._pos -= 1
        self.append_int(s)
        return s

    def create_snapshot(self, frame, is_last=False):
        self._total_snapshots += 1
        array = frame.get_list_of_active_boxes(True, self.new_array, self._add_box_to_storage)
        self.snapshot_add_prev(SNAPSHOT_PREV_COMES_NEXT)
        return self._encode_snapshot(frame.jitcode.index, frame.pc, array, is_last=is_last)

    def snapshot_add_prev(self, prev):
        assert self._snapshot_data[-1] == '}' # == 0x7d == -3 == SNAPSHOT_PREV_NEEDS_PATCHING
        self._snapshot_data.pop()
        self.append_snapshot_data_int(prev)


    # ____________________________________________________________

    def get_iter(self):
        return TraceIterator(self, self._start, self._pos,
                             metainterp_sd=self.metainterp_sd)

    def get_live_ranges(self):
        t = self.get_iter()
        liveranges = [0] * self._index
        index = t._count
        while not t.done():
            index = t.next_element_update_live_range(index, liveranges)
        assert len(self.liveranges) == len(liveranges)
        for index, r in enumerate(self.liveranges):
            assert r >= liveranges[index] - 1
        return liveranges

    def get_dead_ranges(self):
        """ Same as get_live_ranges, but returns a list of "dying" indexes,
        such as for each index x, the number found there is for sure dead
        before x
        """
        def insert(ranges, pos, v):
            # XXX skiplist
            while ranges[pos]:
                pos += 1
                if pos == len(ranges):
                    return
            ranges[pos] = v

        if self._deadranges != (-1, None):
            if self._deadranges[0] == self._count:
                return self._deadranges[1]
        liveranges = self.get_live_ranges()
        deadranges = [0] * (self._index + 2)
        assert len(deadranges) == len(liveranges) + 2
        for i in range(self._start, len(liveranges)):
            elem = liveranges[i]
            if elem:
                insert(deadranges, elem + 1, i)
        self._deadranges = (self._count, deadranges)
        return deadranges

    def unpack(self):
        iter = self.get_iter()
        ops = []
        try:
            while True:
                ops.append(iter.next())
        except IndexError:
            pass
        return iter.inputargs, ops

def tag(kind, pos):
    res = intmask(r_uint(pos) << TAGSHIFT)
    assert res >> TAGSHIFT == pos
    return (pos << TAGSHIFT) | kind

@specialize.ll()
def untag(tagged):
    return intmask(tagged) & TAGMASK, intmask(tagged) >> TAGSHIFT
