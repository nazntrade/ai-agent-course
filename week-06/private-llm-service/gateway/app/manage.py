"""Local administration and short-lived current-user DPAPI pairing."""
import ctypes
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat
import time
import threading

from gateway.app.database import DomainError

_directory_locks={}
_directory_locks_guard=threading.Lock()


@contextmanager
def pairing_transaction(directory):
    """Serialize payload ownership across both threads and trusted processes."""
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/"operations.lock"
    for candidate in (directory,path):
        if candidate.exists():
            info=candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
                raise ValueError("linked_pairing_operation")
    with _directory_locks_guard:
        local=_directory_locks.setdefault(str(directory.resolve()),threading.RLock())
    with local:
        with path.open("a+b") as lock:
            deadline=time.monotonic()+10
            acquired=False
            while not acquired:
                try:
                    if os.name=="nt":
                        import msvcrt
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    acquired=True
                except OSError:
                    if time.monotonic()>=deadline:
                        raise ValueError("pairing_operation_busy") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                if os.name=="nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def serialized_pairing(function):
    @wraps(function)
    def operation(store,directory,*args,**kwargs):
        with pairing_transaction(directory):
            return function(store,directory,*args,**kwargs)
    return operation


class Blob(ctypes.Structure):
    _fields_=[("size",ctypes.c_ulong),("data",ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(payload,unprotect=False):
    if os.name!="nt":
        raise ValueError("windows_dpapi_required")
    buffer=ctypes.create_string_buffer(payload)
    source=Blob(len(payload),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    target=Blob()
    function=ctypes.windll.crypt32.CryptUnprotectData if unprotect else ctypes.windll.crypt32.CryptProtectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(Blob)]
    function.restype=ctypes.c_int
    ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype=ctypes.c_void_p
    if unprotect:
        result=function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    else:
        result=function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    if not result:
        raise ValueError("dpapi_operation_failed")
    try:
        return ctypes.string_at(target.data,target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data,ctypes.c_void_p))


def protect(payload):
    return _crypt(payload)


def unprotect(payload):
    return _crypt(payload,True)


def restrict_directory(directory):
    directory.mkdir(parents=True,exist_ok=True)
    info=directory.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
        raise ValueError("linked_pairing_directory")
    if os.name!="nt":
        raise ValueError("windows_acl_required")
    descriptor=ctypes.c_void_p()
    ctypes.windll.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[
        ctypes.c_wchar_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
    ctypes.windll.advapi32.SetFileSecurityW.argtypes=[ctypes.c_wchar_p,ctypes.c_ulong,ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype=ctypes.c_void_p
    if not ctypes.windll.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;OICI;FA;;;OW)",1,ctypes.byref(descriptor),None):
        raise ValueError("pairing_acl_failed")
    try:
        if not ctypes.windll.advapi32.SetFileSecurityW(str(directory),0x00000004|0x80000000,descriptor):
            raise ValueError("pairing_acl_failed")
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)


def _expire_payload(store,directory):
    metadata=directory/"pending.json"
    if not metadata.exists():
        return
    value=json.loads(metadata.read_text(encoding="utf-8"))
    if value["expires_at"]<=store.clock():
        store.revoke(value["device_id"])
        (directory/"initial-device.dpapi").unlink(missing_ok=True)
        metadata.unlink()


@serialized_pairing
def expire_payload(store,directory):
    _expire_payload(store,directory)


@serialized_pairing
def provision_device(store,directory,owner_id,label,*,protector=protect,restrict=restrict_directory):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",owner_id) or not isinstance(label,str) or not 1<=len(label)<=80:
        raise ValueError("invalid_device_metadata")
    restrict(directory)
    _expire_payload(store,directory)
    target=directory/"initial-device.dpapi"
    if target.exists() or (directory/"pending.json").exists():
        raise ValueError("pending_pairing_exists")
    device_id,token=store.provision(owner_id,label,pairing=True)
    payload={"schema_version":"device-pairing-v1","device_id":device_id,"token":token,"expires_at":store.clock()+900}
    owned_target=False
    owned_metadata=False
    try:
        encrypted=protector(json.dumps(payload).encode())
        with target.open("xb") as file:
            owned_target=True
            file.write(encrypted)
        with (directory/"pending.json").open("x",encoding="utf-8") as file:
            owned_metadata=True
            json.dump({"device_id":device_id,"expires_at":payload["expires_at"]},file)
    except BaseException:
        store.revoke(device_id)
        if owned_target:
            target.unlink(missing_ok=True)
        if owned_metadata:
            (directory/"pending.json").unlink(missing_ok=True)
        raise
    return {"device_id":device_id,"pairing_status":"PENDING","expires_in_seconds":900}


@serialized_pairing
def consume_pairing(store,directory,*,decryptor=unprotect):
    _expire_payload(store,directory)
    target=directory/"initial-device.dpapi"
    if not target.exists() or target.stat().st_size>16384:
        raise ValueError("pairing_unavailable")
    payload=json.loads(decryptor(target.read_bytes()))
    if (payload.get("schema_version")!="device-pairing-v1" or payload.get("expires_at",0)<=store.clock()
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}",payload.get("token",""))):
        raise ValueError("invalid_pairing_payload")
    return payload


@serialized_pairing
def confirm_pairing(store,directory,device_id):
    pending=json.loads((directory/"pending.json").read_text(encoding="utf-8"))
    if pending["device_id"]!=device_id:
        raise ValueError("pairing_device_mismatch")
    store.confirm_pairing(device_id)
    (directory/"initial-device.dpapi").unlink()
    (directory/"pending.json").unlink()


@serialized_pairing
def revoke_device(store,directory,device_id):
    if not re.fullmatch(r"[a-f0-9]{32}",device_id):
        raise ValueError("invalid_device_id")
    store.revoke(device_id)
    pending=directory/"pending.json"
    if pending.exists() and json.loads(pending.read_text(encoding="utf-8"))["device_id"]==device_id:
        (directory/"initial-device.dpapi").unlink(missing_ok=True)
        pending.unlink()
