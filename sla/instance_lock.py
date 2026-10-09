# copyright by berlonak
# telegram: @Kilax123
"""One running monitor per database: a second copy would double every alert,
fight over Bot API getUpdates (409) and share the same Telethon sessions."""
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def lock_path_for(cfg):
    db = Path(cfg["state_db"])
    return db.with_name(db.name + ".lock")


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(
                f"монитор (или whoami) уже запущен с этой базой — занят {self.path.name}. "
                "Останови другой экземпляр и повтори.") from None
        self._handle = handle
        return self

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
