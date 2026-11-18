"""One build process per source tree, including PlatformIO tasks from VS Code."""
import os
from pathlib import Path


def acquire(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    handle.seek(0,os.SEEK_END)
    if handle.tell()==0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError('A build is already running for this firmware. Wait for it to finish; do not run Build/Clean concurrently.') from exc
    return handle  # OS releases the lock on close/process exit, even after a crash.
