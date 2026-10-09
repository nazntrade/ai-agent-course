"""Owned-process CPU and working-set sampling (Windows, no extra dependency)."""
import ctypes
from ctypes import wintypes
import os
import threading
import time

class Memory(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),*[(n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]]

def read_process(pid):
    if os.name!='nt':
        raise RuntimeError('Windows owned-process resource sampling required')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Memory),wintypes.DWORD]
    handle=kernel.OpenProcess(0x0400|0x0010,False,pid)
    if not handle: raise OSError('Cannot sample owned model PID')
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        mem=Memory();mem.cb=ctypes.sizeof(mem)
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]) or not psapi.GetProcessMemoryInfo(handle,ctypes.byref(mem),mem.cb): raise OSError('Resource counter failed')
        cpu=sum((t.dwHighDateTime<<32)+t.dwLowDateTime for t in times[2:])/1e7
        return cpu,mem.WorkingSetSize
    finally:kernel.CloseHandle(handle)

class ResourceSample:
    def __init__(self,pid):self.pid=pid;self.done=threading.Event();self.values=[];self.error=None
    def __enter__(self):
        self.start=time.perf_counter();self.cpu_start,self.ram_start=read_process(self.pid)
        def poll():
            while not self.done.wait(.1):
                try:self.values.append(read_process(self.pid)[1])
                except OSError as exc:self.error=str(exc);break
        self.thread=threading.Thread(target=poll,daemon=True);self.thread.start();return self
    def __exit__(self,*args):
        self.done.set();self.thread.join();cpu,ram=read_process(self.pid);wall=time.perf_counter()-self.start
        self.result={'wall_ms':round(wall*1000,3),'cpu_seconds':round(cpu-self.cpu_start,4),'cpu_percent_machine':round((cpu-self.cpu_start)/wall/(os.cpu_count() or 1)*100,3),'logical_cpu_count':os.cpu_count(),'ram_peak_bytes':max([self.ram_start,ram,*self.values]),'ram_start_bytes':self.ram_start,'sampling_interval_ms':100,'method':'GetProcessTimes user+kernel delta; normalized by wall time and logical CPUs. GetProcessMemoryInfo owned llama-server working set sampled every 100 ms. GPU memory not included.','sampling_error':self.error}
