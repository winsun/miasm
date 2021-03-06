#!/usr/bin/env python
#-*- coding:utf-8 -*-

#
# Copyright (C) 2011 EADS France, Fabrice Desclaux <fabrice.desclaux@eads.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
import logging
import os
import struct

from elfesteem import pe_init

from miasm2.jitter.csts import PAGE_READ, PAGE_WRITE
from miasm2.core.utils import pck32, upck32
import miasm2.arch.x86.regs as x86_regs


# Constants Windows
EXCEPTION_BREAKPOINT = 0x80000003
EXCEPTION_ACCESS_VIOLATION = 0xc0000005
EXCEPTION_INT_DIVIDE_BY_ZERO = 0xc0000094
EXCEPTION_PRIV_INSTRUCTION = 0xc0000096
EXCEPTION_ILLEGAL_INSTRUCTION = 0xc000001d


log = logging.getLogger("seh_helper")
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(levelname)-5s: %(message)s"))
log.addHandler(console_handler)
log.setLevel(logging.INFO)

FS_0_AD = 0x7ff70000
PEB_AD = 0x7ffdf000
LDR_AD = 0x340000

MAX_MODULES = 0x40

# fs:[0] Page (TIB)
tib_address = FS_0_AD
peb_address = PEB_AD
peb_ldr_data_offset = 0x1ea0
peb_ldr_data_address = LDR_AD + peb_ldr_data_offset


modules_list_offset = 0x1f00

InInitializationOrderModuleList_offset = 0x1ee0
InInitializationOrderModuleList_address = LDR_AD + \
    InInitializationOrderModuleList_offset

InLoadOrderModuleList_offset = 0x1ee0 + \
    MAX_MODULES * 0x1000
InLoadOrderModuleList_address = LDR_AD + \
    InLoadOrderModuleList_offset

default_seh = PEB_AD + 0x20000

process_environment_address = 0x10000
process_parameters_address = 0x200000

context_address = 0x201000
exception_record_address = context_address + 0x1000
return_from_exception = 0x6eadbeef

FAKE_SEH_B_AD = context_address + 0x2000

cur_seh_ad = FAKE_SEH_B_AD

loaded_modules = ["ntdll.dll", "kernel32.dll"]
main_pe = None
main_pe_name = "c:\\xxx\\toto.exe"

MAX_SEH = 5


def build_teb(myjit, teb_address):
    """
    +0x000 NtTib                     : _NT_TIB
    +0x01c EnvironmentPointer        : Ptr32 Void
    +0x020 ClientId                  : _CLIENT_ID
    +0x028 ActiveRpcHandle           : Ptr32 Void
    +0x02c ThreadLocalStoragePointer : Ptr32 Void
    +0x030 ProcessEnvironmentBlock   : Ptr32 _PEB
    +0x034 LastErrorValue            : Uint4B
    ...
    """
    o = ""
    o += pck32(default_seh)
    o += (0x18 - len(o)) * "\x00"
    o += pck32(tib_address)

    o += (0x30 - len(o)) * "\x00"
    o += pck32(peb_address)
    o += pck32(0x11223344)

    myjit.vm.add_memory_page(teb_address, PAGE_READ | PAGE_WRITE, o)


def build_peb(myjit, peb_address):
    """
    +0x000 InheritedAddressSpace    : UChar
    +0x001 ReadImageFileExecOptions : UChar
    +0x002 BeingDebugged            : UChar
    +0x003 SpareBool                : UChar
    +0x004 Mutant                   : Ptr32 Void
    +0x008 ImageBaseAddress         : Ptr32 Void
    +0x00c Ldr                      : Ptr32 _PEB_LDR_DATA
    +0x010 processparameter
    """

    offset = peb_address + 8
    o = ""
    if main_pe:
        o += pck32(main_pe.NThdr.ImageBase)
    else:
        offset += 4
    o += pck32(peb_ldr_data_address)
    o += pck32(process_parameters_address)
    myjit.vm.add_memory_page(offset, PAGE_READ | PAGE_WRITE, o)


def build_ldr_data(myjit, modules_info):
    """
    +0x000 Length                          : Uint4B
    +0x004 Initialized                     : UChar
    +0x008 SsHandle                        : Ptr32 Void
    +0x00c InLoadOrderModuleList           : _LIST_ENTRY
    +0x014 InMemoryOrderModuleList         : _LIST_ENTRY
    +0x01C InInitializationOrderModuleList         : _LIST_ENTRY
    """
    o = ""
    # ldr offset pad
    offset = LDR_AD + peb_ldr_data_offset + 0xC

    # get main pe info
    m_e = None
    for bname, (addr, e) in modules_info.items():
        if e == main_pe:
            m_e = (e, bname, addr)
            break
    if not m_e:
        log.warn('No main pe, ldr data will be unconsistant')
        offset, data = offset + 8, ""
    else:
        log.info('Ldr %x', m_e[2])
        data = pck32(m_e[2]) + pck32(0)

    # get ntdll
    ntdll_e = None
    for bname, (addr, e) in modules_info.items():
        if bname[::2].lower() == "ntdll.dll":
            ntdll_e = (e, bname, addr)
            continue
    if not ntdll_e:
        log.warn('No ntdll, ldr data will be unconsistant')
    else:
        data += pck32(ntdll_e[2] + 0x8) + pck32(0)  # XXX TODO
        data += pck32(ntdll_e[2] + 0x10) + pck32(0)

    if data:
        myjit.vm.add_memory_page(offset, PAGE_READ | PAGE_WRITE, data)


dummy_e = pe_init.PE()
dummy_e.NThdr.ImageBase = 0
dummy_e.Opthdr.AddressOfEntryPoint = 0
dummy_e.NThdr.sizeofimage = 0


def create_modules_chain(myjit, modules_name):
    """
    kd> dt nt!_LDR_DATA_TABLE_ENTRY
    +0x000 InLoadOrderLinks : _LIST_ENTRY
    +0x008 InMemoryOrderLinks : _LIST_ENTRY
    +0x010 InInitializationOrderLinks : _LIST_ENTRY
    +0x018 DllBase : Ptr32 Void
    +0x01c EntryPoint : Ptr32 Void
    +0x020 SizeOfImage : Uint4B
    +0x024 FullDllName : _UNICODE_STRING
    +0x02c BaseDllName : _UNICODE_STRING
    +0x034 Flags : Uint4B
    +0x038 LoadCount : Uint2B
    +0x03a TlsIndex : Uint2B
    +0x03c HashLinks : _LIST_ENTRY
    +0x03c SectionPointer : Ptr32 Void
    +0x040 CheckSum : Uint4B
    +0x044 TimeDateStamp : Uint4B
    +0x044 LoadedImports : Ptr32 Void
    +0x048 EntryPointActivationContext : Ptr32 Void
    +0x04c PatchInformation : Ptr32 Void
    """

    modules_info = {}
    base_addr = LDR_AD + modules_list_offset  # XXXX
    offset_name = 0x500
    offset_path = 0x600

    out = ""
    for i, m in enumerate([(main_pe_name, main_pe),
                           ("", dummy_e)] + modules_name):
        addr = base_addr + i * 0x1000
        if isinstance(m, tuple):
            fname, e = m
        else:
            fname, e = m, None
        bpath = fname.replace('/', '\\')
        bname_str = os.path.split(fname)[1].lower()
        bname = "\x00".join(bname_str) + "\x00"
        if e is None:
            if i == 0:
                full_name = fname
            else:
                full_name = os.path.join("win_dll", fname)
            try:
                e = pe_init.PE(open(full_name, 'rb').read())
            except IOError:
                log.error('No main pe, ldr data will be unconsistant!')
                e = None
        if e is None:
            continue
        log.info("Add module %x %r", e.NThdr.ImageBase, bname_str)

        modules_info[bname] = addr, e

        m_o = ""
        m_o += pck32(0)
        m_o += pck32(0)
        m_o += pck32(0)
        m_o += pck32(0)
        m_o += pck32(0)
        m_o += pck32(0)
        m_o += pck32(e.NThdr.ImageBase)
        m_o += pck32(e.rva2virt(e.Opthdr.AddressOfEntryPoint))
        m_o += pck32(e.NThdr.sizeofimage)
        m_o += struct.pack('HH', len(bname), len(bname) + 2)
        m_o += pck32(addr + offset_path)
        m_o += struct.pack('HH', len(bname), len(bname) + 2)
        m_o += pck32(addr + offset_name)
        myjit.vm.add_memory_page(addr, PAGE_READ | PAGE_WRITE, m_o)

        m_o = ""
        m_o += bname
        m_o += "\x00" * 3
        myjit.vm.add_memory_page(
            addr + offset_name, PAGE_READ | PAGE_WRITE, m_o)

        m_o = ""
        m_o += "\x00".join(bpath) + "\x00"
        m_o += "\x00" * 3
        myjit.vm.add_memory_page(
            addr + offset_path, PAGE_READ | PAGE_WRITE, m_o)

    return modules_info


def fix_InLoadOrderModuleList(myjit, module_info):
    log.debug("Fix InLoadOrderModuleList")
    # first binary is PE
    # last is dumm_e
    olist = []
    m_e = None
    d_e = None
    for m in [main_pe_name, ""] + loaded_modules:

        if isinstance(m, tuple):
            fname, e = m
        else:
            fname, e = m, None

        if "/" in fname:
            fname = fname[fname.rfind("/") + 1:]
        bname_str = fname
        bname = '\x00'.join(bname_str) + '\x00'
        if not bname.lower() in module_info:
            log.warn('Module not found, ldr data will be unconsistant')
            continue

        addr, e = module_info[bname.lower()]
        log.debug(bname_str)
        if e == main_pe:
            m_e = (e, bname, addr)
            continue
        elif e == dummy_e:
            d_e = (e, bname, addr)
            continue
        olist.append((e, bname, addr))
    if not m_e or not d_e:
        log.warn('No main pe, ldr data will be unconsistant')
    else:
        olist[0:0] = [m_e]
    olist.append(d_e)

    last_addr = 0
    for i in xrange(len(olist)):
        e, bname, addr = olist[i]
        p_e, p_bname, p_addr = olist[(i - 1) % len(olist)]
        n_e, n_bname, n_addr = olist[(i + 1) % len(olist)]
        myjit.vm.set_mem(addr + 0, pck32(n_addr) + pck32(p_addr))


def fix_InMemoryOrderModuleList(myjit, module_info):
    log.debug("Fix InMemoryOrderModuleList")
    # first binary is PE
    # last is dumm_e
    olist = []
    m_e = None
    d_e = None
    for m in [main_pe_name, ""] + loaded_modules:

        if isinstance(m, tuple):
            fname, e = m
        else:
            fname, e = m, None

        if "/" in fname:
            fname = fname[fname.rfind("/") + 1:]
        bname_str = fname
        bname = '\x00'.join(bname_str) + '\x00'
        if not bname.lower() in module_info:
            log.warn('Module not found, ldr data will be unconsistant')
            continue
        addr, e = module_info[bname.lower()]
        log.debug(bname_str)
        if e == main_pe:
            m_e = (e, bname, addr)
            continue
        elif e == dummy_e:
            d_e = (e, bname, addr)
            continue
        olist.append((e, bname, addr))
    if not m_e or not d_e:
        log.warn('No main pe, ldr data will be unconsistant')
    else:
        olist[0:0] = [m_e]
    olist.append(d_e)

    last_addr = 0

    for i in xrange(len(olist)):
        e, bname, addr = olist[i]
        p_e, p_bname, p_addr = olist[(i - 1) % len(olist)]
        n_e, n_bname, n_addr = olist[(i + 1) % len(olist)]
        myjit.vm.set_mem(
            addr + 0x8, pck32(n_addr + 0x8) + pck32(p_addr + 0x8))


def fix_InInitializationOrderModuleList(myjit, module_info):
    # first binary is ntdll
    # second binary is kernel32
    olist = []
    ntdll_e = None
    kernel_e = None
    for bname, (addr, e) in module_info.items():
        if bname[::2].lower() == "ntdll.dll":
            ntdll_e = (e, bname, addr)
            continue
        elif bname[::2].lower() == "kernel32.dll":
            kernel_e = (e, bname, addr)
            continue
        elif e == dummy_e:
            d_e = (e, bname, addr)
            continue
        elif e == main_pe:
            continue
        olist.append((e, bname, addr))
    if not ntdll_e or not kernel_e or not d_e:
        log.warn('No kernel ntdll, ldr data will be unconsistant')
    else:
        olist[0:0] = [ntdll_e]
        olist[1:1] = [kernel_e]

    olist.append(d_e)

    last_addr = 0
    for i in xrange(len(olist)):
        e, bname, addr = olist[i]
        p_e, p_bname, p_addr = olist[(i - 1) % len(olist)]
        n_e, n_bname, n_addr = olist[(i + 1) % len(olist)]
        myjit.vm.set_mem(
            addr + 0x10, pck32(n_addr + 0x10) + pck32(p_addr + 0x10))


def add_process_env(myjit):
    env_str = 'ALLUSEESPROFILE=C:\\Documents and Settings\\All Users\x00'
    env_str = '\x00'.join(env_str)
    env_str += "\x00" * 0x10
    myjit.vm.add_memory_page(process_environment_address,
                             PAGE_READ | PAGE_WRITE,
                             env_str)
    myjit.vm.set_mem(process_environment_address, env_str)


def add_process_parameters(myjit):
    o = ""
    o += pck32(0x1000)  # size
    o += "E" * (0x48 - len(o))
    o += pck32(process_environment_address)
    myjit.vm.add_memory_page(process_parameters_address,
                             PAGE_READ | PAGE_WRITE,
                             o)


all_seh_ad = dict([(x, None)
                  for x in xrange(FAKE_SEH_B_AD, FAKE_SEH_B_AD + 0x1000, 0x20)])
# http://blog.fireeye.com/research/2010/08/download_exec_notes.html
seh_count = 0


def init_seh(myjit):
    global seh_count
    seh_count = 0
    build_teb(myjit, FS_0_AD)
    build_peb(myjit, peb_address)

    module_info = create_modules_chain(myjit, loaded_modules)
    fix_InLoadOrderModuleList(myjit, module_info)
    fix_InMemoryOrderModuleList(myjit, module_info)
    fix_InInitializationOrderModuleList(myjit, module_info)

    build_ldr_data(myjit, module_info)
    add_process_env(myjit)
    add_process_parameters(myjit)

    myjit.vm.add_memory_page(default_seh, PAGE_READ | PAGE_WRITE, pck32(
        0xffffffff) + pck32(0x41414141) + pck32(0x42424242))

    myjit.vm.add_memory_page(
        context_address, PAGE_READ | PAGE_WRITE, '\x00' * 0x2cc)
    myjit.vm.add_memory_page(
        exception_record_address, PAGE_READ | PAGE_WRITE, '\x00' * 200)

    myjit.vm.add_memory_page(
        FAKE_SEH_B_AD, PAGE_READ | PAGE_WRITE, 0x10000 * "\x00")

# http://www.codeproject.com/KB/system/inject2exe.aspx#RestorethefirstRegistersContext5_1


def regs2ctxt(myjit):
    """
    Build x86_32 cpu context for exception handling
    @myjit: jitload instance
    """

    ctxt = []
    # ContextFlags
    ctxt += [pck32(0x0)]
    # DRX
    ctxt += [pck32(0x0)] * 6
    # Float context
    ctxt += ['\x00' * 112]
    # Segment selectors
    ctxt += [pck32(reg) for reg in (myjit.cpu.GS, myjit.cpu.FS,
                                    myjit.cpu.ES, myjit.cpu.DS)]
    # Gpregs
    ctxt += [pck32(reg) for reg in (myjit.cpu.EDI, myjit.cpu.ESI,
                                    myjit.cpu.EBX, myjit.cpu.EDX,
                                    myjit.cpu.ECX, myjit.cpu.EAX,
                                    myjit.cpu.EBP, myjit.cpu.EIP)]
    # CS
    ctxt += [pck32(myjit.cpu.CS)]
    # Eflags
    # XXX TODO real eflag
    ctxt += [pck32(0x0)]
    # ESP
    ctxt += [pck32(myjit.cpu.ESP)]
    # SS
    ctxt += [pck32(myjit.cpu.SS)]
    return "".join(ctxt)


def ctxt2regs(ctxt, myjit):
    """
    Restore x86_32 registers from an exception context
    @ctxt: the serialized context
    @myjit: jitload instance
    """

    ctxt = ctxt[:]
    # ContextFlags
    ctxt = ctxt[4:]
    # DRX XXX TODO
    ctxt = ctxt[4 * 6:]
    # Float context XXX TODO
    ctxt = ctxt[112:]
    # gs
    myjit.cpu.GS = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    # fs
    myjit.cpu.FS = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    # es
    myjit.cpu.ES = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    # ds
    myjit.cpu.DS = upck32(ctxt[:4])
    ctxt = ctxt[4:]

    # Gpregs
    myjit.cpu.EDI = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.ESI = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.EBX = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.EDX = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.ECX = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.EAX = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.EBP = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    myjit.cpu.EIP = upck32(ctxt[:4])
    ctxt = ctxt[4:]

    # CS
    myjit.cpu.CS = upck32(ctxt[:4])
    ctxt = ctxt[4:]
    # Eflag XXX TODO
    ctxt = ctxt[4:]
    # ESP
    myjit.cpu.ESP = upck32(ctxt[:4])
    ctxt = ctxt[4:]


def fake_seh_handler(myjit, except_code):
    global seh_count, context_address
    regs = myjit.cpu.get_gpreg()
    log.warning('Exception at %x %r', myjit.cpu.EIP, seh_count)
    seh_count += 1

    # Help lambda
    p = lambda s: struct.pack('I', s)

    # Forge a CONTEXT
    ctxt = regs2ctxt(myjit)

    # Get current seh (fs:[0])
    seh_ptr = upck32(myjit.vm.get_mem(tib_address, 4))

    # Retrieve seh fields
    old_seh, eh, safe_place = struct.unpack(
        'III', myjit.vm.get_mem(seh_ptr, 0xc))

    # Get space on stack for exception handling
    myjit.cpu.ESP -= 0x3c8
    exception_base_address = myjit.cpu.ESP
    exception_record_address = exception_base_address + 0xe8
    context_address = exception_base_address + 0xfc
    fake_seh_address = exception_base_address + 0x14

    log.info('seh_ptr %x { old_seh %x eh %x safe_place %x} ctx_addr %x',
             seh_ptr, old_seh, eh, safe_place, context_address)

    # Write context
    myjit.vm.set_mem(context_address, ctxt)

    # Write exception_record

    """
    #http://msdn.microsoft.com/en-us/library/aa363082(v=vs.85).aspx

    typedef struct _EXCEPTION_RECORD {
      DWORD                    ExceptionCode;
      DWORD                    ExceptionFlags;
      struct _EXCEPTION_RECORD *ExceptionRecord;
      PVOID                    ExceptionAddress;
      DWORD                    NumberParameters;
      ULONG_PTR ExceptionInformation[EXCEPTION_MAXIMUM_PARAMETERS];
    } EXCEPTION_RECORD, *PEXCEPTION_RECORD;
    """

    myjit.vm.set_mem(exception_record_address,
                     pck32(except_code) + pck32(0) + pck32(0) +
                     pck32(myjit.cpu.EIP) + pck32(0))

    # Prepare the stack
    myjit.push_uint32_t(context_address)               # Context
    myjit.push_uint32_t(seh_ptr)                       # SEH
    myjit.push_uint32_t(exception_record_address)      # ExceptRecords
    myjit.push_uint32_t(return_from_exception)         # Ret address

    # Set fake new current seh for exception
    log.info("Fake seh ad %x", fake_seh_address)
    myjit.vm.set_mem(fake_seh_address, pck32(seh_ptr) + pck32(
        0xaaaaaaaa) + pck32(0xaaaaaabb) + pck32(0xaaaaaacc))
    myjit.vm.set_mem(tib_address, pck32(fake_seh_address))

    dump_seh(myjit)

    log.info('Jumping at %x', eh)
    myjit.vm.set_exception(0)
    myjit.cpu.set_exception(0)

    # XXX set ebx to nul?
    myjit.cpu.EBX = 0

    return eh

fake_seh_handler.base = FAKE_SEH_B_AD


def dump_seh(myjit):
    log.info('Dump_seh. Tib_address: %x', tib_address)
    cur_seh_ptr = upck32(myjit.vm.get_mem(tib_address, 4))
    indent = 1
    loop = 0
    while True:
        if loop > MAX_SEH:
            log.warn("Too many seh, quit")
            return
        prev_seh, eh = struct.unpack('II', myjit.vm.get_mem(cur_seh_ptr, 8))
        log.info('\t' * indent + 'seh_ptr: %x { prev_seh: %x eh %x }',
                 cur_seh_ptr, prev_seh, eh)
        if prev_seh in [0xFFFFFFFF, 0]:
            break
        cur_seh_ptr = prev_seh
        indent += 1
        loop += 1


def set_win_fs_0(myjit, fs=4):
    regs = myjit.cpu.get_gpreg()
    regs['FS'] = 0x4
    myjit.cpu.set_gpreg(regs)
    myjit.cpu.set_segm_base(regs['FS'], FS_0_AD)
    segm_to_do = set([x86_regs.FS])
    return segm_to_do


def add_modules_info(pe_in, pe_in_name="toto.exe", all_pe=None):
    global main_pe, main_pe_name, loaded_modules
    if all_pe is None:
        all_pe = []
    main_pe = pe_in
    main_pe_name = pe_in_name
    loaded_modules = all_pe


def return_from_seh(myjit):
    "Handle return after a call to fake seh handler"

    # Get current context
    context_address = upck32(myjit.vm.get_mem(myjit.cpu.ESP + 0x8, 4))
    log.info('Context address: %x', context_address)
    myjit.cpu.ESP = upck32(myjit.vm.get_mem(context_address + 0xc4, 4))
    log.info('New esp: %x', myjit.cpu.ESP)

    # Rebuild SEH
    old_seh = upck32(myjit.vm.get_mem(tib_address, 4))
    new_seh = upck32(myjit.vm.get_mem(old_seh, 4))
    log.info('Old seh: %x New seh: %x', old_seh, new_seh)
    myjit.vm.set_mem(tib_address, pck32(new_seh))

    dump_seh(myjit)

    if myjit.cpu.EAX == 0x0:
        # ExceptionContinueExecution
        ctxt_ptr = context_address
        log.info('Seh continues Context: %x', ctxt_ptr)

        # Get registers changes
        ctxt_str = myjit.vm.get_mem(ctxt_ptr, 0x2cc)
        ctxt2regs(ctxt_str, myjit)
        myjit.pc = myjit.cpu.EIP
        log.info('Context::Eip: %x', myjit.pc)

    elif myjit.cpu.EAX == -1:
        raise NotImplementedError("-> seh try to go to the next handler")

    elif myjit.cpu.EAX == 1:
        # ExceptionContinueSearch
        raise NotImplementedError("-> seh, gameover")
