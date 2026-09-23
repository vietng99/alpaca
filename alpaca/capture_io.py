"""Crash-tolerant private JSONL append with a persistent inode and bounded records."""
import fcntl
import json
import os
from pathlib import Path
import stat
import time


def checked_path(root, path):
    root = Path(root).resolve()
    path = Path(path).absolute()
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        if part in ('.','..'):
            raise ValueError('unsafe capture path')
        current = current/part
        if current.is_symlink():
            raise ValueError('capture symlink refused')
    path.resolve().relative_to(root)
    return path


def append_json(path, record, *, root=None):
    path = checked_path(root,path) if root is not None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(record, ensure_ascii=False, default=str, separators=(',',':'))+'\n').encode()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os,'O_NOFOLLOW',0) | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('capture destination must be a regular file')
        until = time.monotonic()+0.5
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError('capture lock timed out')
                time.sleep(0.005)
        size = os.fstat(fd).st_size
        if size and os.pread(fd,1,size-1) != b'\n':
            data = b'\n'+data  # isolate a previous interrupted append
        view = data
        while view:
            count = os.write(fd,view)
            if count <= 0:
                raise OSError('capture append made no progress')
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(path.parent,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
