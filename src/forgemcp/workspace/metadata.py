"""Platform-specific file ownership; unavailable accounts are represented by None."""

import os
from pathlib import Path


def file_owner(path: Path) -> str | None:
    if os.name != "nt":
        try:
            return path.owner()
        except (KeyError, OSError, NotImplementedError):
            return None

    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    get_security = advapi.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    lookup = advapi.LookupAccountSidW
    lookup.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    lookup.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if get_security(
        str(path),
        1,
        1,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    ):
        return None
    try:
        name_size, domain_size, kind = (
            wintypes.DWORD(),
            wintypes.DWORD(),
            wintypes.DWORD(),
        )
        lookup(
            None,
            owner,
            None,
            ctypes.byref(name_size),
            None,
            ctypes.byref(domain_size),
            ctypes.byref(kind),
        )
        if not name_size.value:
            return None
        name = ctypes.create_unicode_buffer(name_size.value)
        domain = ctypes.create_unicode_buffer(max(1, domain_size.value))
        if not lookup(
            None,
            owner,
            name,
            ctypes.byref(name_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(kind),
        ):
            return None
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        kernel.LocalFree(descriptor)
