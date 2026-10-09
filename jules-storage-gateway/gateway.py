#!/usr/bin/env python3
import os
import sys
import errno
import hashlib
import sqlite3
import shutil
import time
from fuse import FUSE, FuseOSError, Operations

JULES_ZONE = "JULES_ZONE"
HIDDEN_BACKUP = "__JULES_BACKUP"

class StorageGateway(Operations):
    def __init__(self, root):
        self.root = root
        self.backup_dir = os.path.join(self.root, HIDDEN_BACKUP)
        self.objects_dir = os.path.join(self.backup_dir, "objects")

        # Ensure backup structure exists
        os.makedirs(self.objects_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        self.db_path = os.path.join(self.backup_dir, "metadata.db")
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS backups
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          original_path TEXT,
                          object_hash TEXT,
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                          operation TEXT)''')
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error initializing DB: {e}")

    def _log_backup(self, path, obj_hash, operation):
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("INSERT INTO backups (original_path, object_hash, operation) VALUES (?, ?, ?)",
                      (path, obj_hash, operation))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error logging backup: {e}")

    def _hash_file(self, filepath):
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as afile:
                buf = afile.read(65536)
                while len(buf) > 0:
                    hasher.update(buf)
                    buf = afile.read(65536)
            return hasher.hexdigest()
        except Exception:
            return None

    def _backup_file_if_needed(self, path, operation="DELETE"):
        full_path = self._full_path(path)
        if not os.path.isfile(full_path):
            return True # Only backup files, not dirs for now

        print(f"Backup Engine: Intercepted {operation} for {path}")

        time.sleep(0.5)

        obj_hash = self._hash_file(full_path)
        if not obj_hash:
            print(f"Backup Engine: Error hashing file {path}")
            return False

        target_obj_path = os.path.join(self.objects_dir, obj_hash)

        try:
            if not os.path.exists(target_obj_path):
                print(f"Backup Engine: Saving new content chunk {obj_hash[:8]}...")
                with open(full_path, 'rb') as f_src, open(target_obj_path, 'wb') as f_dst:
                    shutil.copyfileobj(f_src, f_dst)
            else:
                print(f"Backup Engine: Content already backed up (Dedup). Hash: {obj_hash[:8]}")

            self._log_backup(path, obj_hash, operation)
            return True
        except Exception as e:
            print(f"Backup Engine: Critical error backing up file: {e}")
            return False


    # Helpers
    # =======

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.join(self.root, partial)
        return path

    def _check_access(self, partial, is_delete=False):
        """Enforces the Gateway Policy"""
        if partial.startswith("/"):
            partial = partial[1:]

        parts = partial.split(os.sep)
        first_dir = parts[0] if parts else ""

        # Policy 1: Hidden backup store is strictly inaccessible
        if first_dir == HIDDEN_BACKUP:
            raise FuseOSError(errno.EACCES)

        # Policy 2: Prevent deletion outside of JULES_ZONE
        if is_delete and first_dir != JULES_ZONE:
            print(f"Policy Engine: DENIED DELETE outside {JULES_ZONE} for {partial}")
            raise FuseOSError(errno.EACCES)

    # Filesystem methods
    # ==================

    def access(self, path, mode):
        self._check_access(path)
        full_path = self._full_path(path)
        if not os.access(full_path, mode):
            raise FuseOSError(errno.EACCES)

    def getattr(self, path, fh=None):
        self._check_access(path)
        full_path = self._full_path(path)
        try:
            st = os.lstat(full_path)
        except FileNotFoundError:
            raise FuseOSError(errno.ENOENT)

        return dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                     'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))

    def readdir(self, path, fh):
        self._check_access(path)
        full_path = self._full_path(path)
        dirents = ['.', '..']
        if os.path.isdir(full_path):
            dirents.extend(os.listdir(full_path))

        # Hide __JULES_BACKUP from readdir at root
        if path == "/":
            if HIDDEN_BACKUP in dirents:
                dirents.remove(HIDDEN_BACKUP)

        for r in dirents:
            yield r

    def rmdir(self, path):
        self._check_access(path, is_delete=True)
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        self._check_access(path)
        return os.mkdir(self._full_path(path), mode)

    def statfs(self, path):
        self._check_access(path)
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    def unlink(self, path):
        self._check_access(path, is_delete=True)

        # Backup before logical deletion inside JULES_ZONE
        if path.startswith("/" + JULES_ZONE) or path.startswith(JULES_ZONE):
            success = self._backup_file_if_needed(path, operation="DELETE")
            if not success:
                print(f"Backup Engine: FAILED to backup {path}, aborting unlink.")
                raise FuseOSError(errno.EIO)

        return os.unlink(self._full_path(path))

    def rename(self, old, new):
        self._check_access(old)
        self._check_access(new)

        # In a rename operation, the destination file ('new') is effectively overwritten (deleted)
        # if it already exists. We must trigger a backup of the destination if it exists inside JULES_ZONE.
        if os.path.exists(self._full_path(new)):
            if new.startswith("/" + JULES_ZONE) or new.startswith(JULES_ZONE):
                success = self._backup_file_if_needed(new, operation="OVERWRITE_VIA_RENAME")
                if not success:
                    print(f"Backup Engine: FAILED to backup destination {new}, aborting rename.")
                    raise FuseOSError(errno.EIO)
            else:
                # If the destination exists OUTSIDE JULES_ZONE, this is effectively a deletion
                # of the destination file which is forbidden outside the zone!
                print(f"Policy Engine: DENIED rename overwrite outside {JULES_ZONE} for {new}")
                raise FuseOSError(errno.EACCES)

        return os.rename(self._full_path(old), self._full_path(new))

    def open(self, path, flags):
        self._check_access(path)

        # If open with O_TRUNC (overwrite), trigger backup
        if flags & os.O_TRUNC:
            if path.startswith("/" + JULES_ZONE) or path.startswith(JULES_ZONE):
                success = self._backup_file_if_needed(path, operation="OVERWRITE")
                if not success:
                    print(f"Backup Engine: FAILED to backup {path}, aborting open with O_TRUNC.")
                    raise FuseOSError(errno.EIO)
            elif os.path.exists(self._full_path(path)):
                # If we are overwriting/truncating an EXISTING file outside JULES_ZONE, this is basically a deletion!
                print(f"Policy Engine: DENIED O_TRUNC overwrite outside {JULES_ZONE} for {path}")
                raise FuseOSError(errno.EACCES)

        full_path = self._full_path(path)
        return os.open(full_path, flags)

    def create(self, path, mode, fi=None):
        self._check_access(path)
        full_path = self._full_path(path)
        return os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)


def main(mountpoint, root):
    FUSE(StorageGateway(root), mountpoint, nothreads=True, foreground=True, allow_other=True)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <mountpoint> <underlying_root>")
        sys.exit(1)

    mountpoint = sys.argv[1]
    root = sys.argv[2]

    print(f"Starting Gateway Deduplicating Backup Engine. Root: {root}, Mount: {mountpoint}")
    main(mountpoint, root)
