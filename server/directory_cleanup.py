"""Remove contents within a caller-verified Linux directory binding."""

import ctypes
import os
import stat

OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_RENAME_NOREPLACE = 1
_renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
_renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
_renameat2.restype = ctypes.c_int


class DirectoryBindingError(OSError):
    """Recursive cleanup encountered an unverified directory."""


def mount_id(fd: int) -> str:
    with open(f"/proc/self/fdinfo/{fd}") as info:
        for line in info:
            if line.startswith("mnt_id:"):
                return line.split()[1]
    raise DirectoryBindingError("Directory mount could not be verified")


def move_unoccupied(source_parent: int, source: str, dest_parent: int, dest: str) -> None:
    if _renameat2(source_parent, os.fsencode(source), dest_parent, os.fsencode(dest), _RENAME_NOREPLACE):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), source, None, dest)


def verify_tree_mounts(directory: int, mount: str) -> None:
    """Verify a stopped private tree before copying any file it contains."""
    for name in os.listdir(directory):
        descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if mount_id(descriptor) != mount:
                raise DirectoryBindingError("Private Agent tree crosses a mount")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(".", OPEN_DIRECTORY, dir_fd=descriptor)
                try:
                    verify_tree_mounts(child, mount)
                finally:
                    os.close(child)
        finally:
            os.close(descriptor)


def empty_directory(target: int, mount: str, *, make_writable: bool = False) -> None:
    # Fork copies can inherit read-only modes from their source. Agent memory
    # keeps its original modes and reports a retryable permission failure.
    if make_writable:
        mode = stat.S_IMODE(os.fstat(target).st_mode)
        if not mode & stat.S_IWUSR:
            os.fchmod(target, mode | stat.S_IWUSR)
    for name in os.listdir(target):
        info = os.stat(name, dir_fd=target, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=target)
            continue
        child = os.open(name, OPEN_DIRECTORY, dir_fd=target)
        try:
            if mount_id(child) != mount or not os.path.samestat(info, os.fstat(child)):
                raise DirectoryBindingError("Cleanup encountered a changed or mounted directory")
            empty_directory(child, mount, make_writable=make_writable)
            if not os.path.samestat(info, os.stat(name, dir_fd=target, follow_symlinks=False)):
                raise DirectoryBindingError("Cleanup directory changed during removal")
            os.rmdir(name, dir_fd=target)
        finally:
            os.close(child)
