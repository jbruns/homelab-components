"""Writing a file in place of another without following anything planted at a predictable path."""

import contextlib
import os
import tempfile


def replace_file(dest, text, mode=0o644):
    """Atomically replaces `dest` with `text`: a uniquely named file created beside it (O_EXCL, never through a
    symlink), written, synced and renamed over it. Removes the temp file on failure."""
    fd, temp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, mode)
        os.replace(temp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
