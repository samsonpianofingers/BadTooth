from . import kernel32
from . import ntdll
from . import winnt_constants

import struct


class Process(object):
    """
    class Process(object)
     | Process(process_id) -> Process
     | 
     | Create a new process object which opens a handle
     | to the process specified by process_id
     | Process will automatically close the handle upon destruction.
    """
    def __init__(self, process_id):
        """
        Process(process_id) -> Process
        internal variables:
        process_id - ID of target process
        handle - handle to target process
        patches - dictionary of all loaded patches
        hooks - dictionary of all loaded hooks
        """
        self.process_id = process_id
        self.handle = kernel32.OpenProcess(process_id)
        self.patches = {}
        self.hooks = {}

    def __del__(self):
        """
        Calls Kernel32.dll->CloseHandle(self.handle) upon destruction
        """
        kernel32.CloseHandle(self.handle)

    def read(self, address, n_bytes):
        """
        Read specified bytes from the target process

        Process.read(address, n_bytes) -> buffer: bytearray

        address is the address in the process memory to read from
        n_bytes is the number of bytes to be read
        If the backend api fails it can partially fail and return less bytes than intended.
        Will fail if memory range crosses into a PAGE_NOACCESS memory region etc.
        """
        return kernel32.ReadProcessMemory(self.handle, address, n_bytes)

    def write(self, address, buffer):
        """
        Write a buffer to the target process at the specified address

        Process.write(address, buffer) -> success: bool

        address is the address in the process memory to write to
        buffer is the bytes you want to write to the process
        """
        return kernel32.WriteProcessMemory(self.handle, address, buffer)

    def alloc_rwx(self, size):
        """
        Allocate memory to the target process
        Memory has read/write/execute permissions

        Process.alloc_rwx(size) -> address: int

        """
        return kernel32.VirtualAllocEx(self.handle, 0, size)

    def alloc_rw(self, size):
        """
        Allocate memory to the target process
        Memory has read/write permissions

        Process.alloc_rwx(size) -> address: int
        """
        return kernel32.VirtualAllocEx(self.handle, 0, size,
                                       protect=kernel32.PAGE_READWRITE)

    def free(self, address):
        """
        Releases committed memory from the target process

        Process.free(address) -> success: bool
        """
        return kernel32.VirtualFreeEx(self.handle, address)

    def yield_threads(self):
        """
        Yields threads one by one using a generator object
        Each thread is a THREADENTRY32 structure object
        Threads belong to the target process
        
        Process.yield_threads() -> Generator(kernel32.THREADENTRY32)
        """
        hSnapshot = kernel32.CreateToolhelp32Snapshot(
            winnt_constants.TH32CS_SNAPTHREAD, 0)
        thread_entry = kernel32.Thread32First(hSnapshot)
        yield thread_entry
        while kernel32.Thread32Next(hSnapshot, thread_entry):
            yield thread_entry

    def suspend(self):
        """
        Suspends all running threads in target process

        Process.suspend() -> None
        """
        for thread in self.yield_threads():
            if thread.get_owner_pid() == self.process_id:
                thread_handle = kernel32.OpenThread(thread.get_tid())
                kernel32.SuspendThread(thread_handle)
                kernel32.CloseHandle(thread_handle)

    def resume(self):
        """
        Resumes all suspended threads in target process

        Process.suspend() -> None
        """
        for thread in self.yield_threads():
            if thread.get_owner_pid() == self.process_id:
                thread_handle = kernel32.OpenThread(thread.get_tid())
                kernel32.ResumeThread(thread_handle)
                kernel32.CloseHandle(thread_handle)

    def yield_modules(self):
        """
        Yields modules one by one using a generator object
        Each module is a MODULEENTRY32 structure object
        Modules belong to the target process
        
        Process.yield_modules() -> Generator(kernel32.MODULEENTRY32)
        """
        hSnapshot = kernel32.CreateToolhelp32Snapshot(winnt_constants.TH32CS_SNAPMODULE, self.process_id)
        module_entry = kernel32.Module32First(hSnapshot)
        yield module_entry
        while kernel32.Module32Next(hSnapshot, module_entry):
            yield module_entry

    def yield_memory_regions(self, state=None, protect=None, m_type=None):
        """
        Yields memory regions one by one using a generator object
        Each region is a MEMORY_BASIC_INFORMATION structure object
        Regions belong to the target process
        
        Process.yield_regions(state=None, protect=None, m_type = None) -> Generator(kernel32.MEMORY_BASIC_INFORMATION)]

        Each overload (state, protect, m_type) allows you to filter for certain
        types of memory, you can have any combination of the three filters.

        state can be -> MEM_COMMIT, MEM_FREE, MEM_RESERVE
        
        protect can be a number of things, reference here:
        https://docs.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-memory_basic_information
        
        m_type can be MEM_IMAGE (Regions mapped from exe/dll files), MEM_MAPPED or MEM_PRIVATE

        more information on memory types here:
        https://docs.microsoft.com/en-us/windows/win32/memory/memory-protection-constants

        """
        system_info = kernel32.GetSystemInfo()
        min_address = system_info.lpMinimumApplicationAddress
        max_address = system_info.lpMaximumApplicationAddress
        mem_basic_info = kernel32.VirtualQueryEx(
            self.handle, min_address)

        while mem_basic_info is not None:
            bState = True
            bProtect = True
            bType = True
            if state:
                bState = mem_basic_info.State == state
            if protect:
                bProtect = mem_basic_info.Protect == protect
            if m_type:
                bType = mem_basic_info.Type == m_type
            if bState and bProtect and bType:
                yield mem_basic_info
            address = mem_basic_info.BaseAddress + mem_basic_info.RegionSize
            if address > max_address:
                break
            mem_basic_info = kernel32.VirtualQueryEx(
                self.handle, address)

    def create_thread(self, address, parameter=0):
        """
        Creates a thread in the target process at specified address, default parameter is NULL
        Parameter can be a pointer to some variable for the code that is executed to use.

        Process.create_thread(address) -> thread_handle: HANDLE
        Process.create_thread(address, parameter=0) -> thread_handle: HANDLE
        """
        return kernel32.CreateRemoteThreadEx(self.handle, address, parameter)

    def add_patch(self, patch_name, address, instructions):
        old_data = self.read(address, len(instructions))
        self.write(address, instructions)
        self.patches[patch_name] = (address, old_data)

    def toggle_patch(self, patch_name):
        address, old_data = self.patches[patch_name]
        patch_size = len(old_data)
        patch_address = address
        patch_instructions = self.read(patch_address, patch_size)
        self.write(patch_address, old_data)
        self.patches[patch_name] = (patch_address, patch_instructions)

    def detour_hook(self, target_address, hook_address, instr_length):
        if is_process_32bit(self.handle):
            nops = b''
            if instr_length > 5:
                nops = b'\x90' * (instr_length - 5)
            old_protect = kernel32.VirtualProtectEx(
                self.handle, hook_address, instr_length, kernel32.PAGE_EXECUTE_READWRITE)
            hook_relative = target_address - hook_address - 5
            hook = b'\xE9' + struct.pack("i", hook_relative) + nops
            old_bytes = self.read(hook_address, instr_length)
            self.write(hook_address, hook)
            kernel32.VirtualProtectEx(
                self.handle, hook_address, instr_length, old_protect)
            return old_bytes
        else:
            nops = b''
            if instr_length > 14:
                nops = b'\x90' * (instr_length - 14)
            old_protect = kernel32.VirtualProtectEx(
                self.handle, hook_address, instr_length, kernel32.PAGE_EXECUTE_READWRITE)
            hook = b'\xFF\x25\x00\x00\x00\x00' + \
                struct.pack("Q", target_address) + nops
            old_bytes = self.read(hook_address, instr_length)
            self.write(hook_address, hook)
            kernel32.VirtualProtectEx(
                self.handle, hook_address, instr_length, old_protect)
            return old_bytes

    def add_hook(self, hook_name, hook_address, hook_instr_len, new_code):
        self.suspend()
        target_address = self.alloc_rwx(len(new_code))
        if is_process_32bit(self.handle):
            hook_relative = hook_address - (target_address + len(new_code))
            new_code = new_code + b'\xE9' + struct.pack("i", hook_relative)
        else:
            new_code = new_code + b'\xFF\x25\x00\x00\x00\x00' + \
                struct.pack("Q", hook_address + hook_instr_len)
        self.write(target_address, new_code)
        old_bytes = self.detour_hook(
            target_address, hook_address, hook_instr_len)
        self.resume()
        self.hooks[hook_name] = (hook_address, old_bytes, target_address)

    def remove_hook(self, hook_name):
        hook_address, old_bytes, target_address = self.hooks[hook_name]
        self.suspend()
        self.write(hook_address, old_bytes)
        self.free(target_address)
        self.resume()
        self.hooks.pop(hook_name)


def yield_processes():
    hSnapshot = kernel32.CreateToolhelp32Snapshot(
        winnt_constants.TH32CS_SNAPPROCESS, 0)
    proc_entry = kernel32.Process32First(hSnapshot)
    yield proc_entry
    while kernel32.Process32Next(hSnapshot, proc_entry):
        yield proc_entry


def get_process_first(process_name):
    for process in yield_processes():
        curr_process_name = process.get_name()
        if curr_process_name.find(process_name) != -1:
            return process


def get_processes(process_name):
    process_list = []
    for process in yield_processes():
        curr_process_name = process.get_name()
        if curr_process_name.find(process_name) != -1:
            process_list.append(process)
    return process_list


def enable_sedebug():
    ntdll.AdjustPrivilege(
        ntdll.SE_DEBUG_PRIVILEGE, True)


def is_process_32bit(handle):
    return kernel32.IsWow64Process(handle)
