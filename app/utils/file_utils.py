import os
import hashlib
import re
from pathlib import Path

def sanitize_filename(filename: str) -> str:
    """Remove path traversal characters and special chars."""
    if not filename:
        return ""
    # Strip path traversal elements
    filename = os.path.basename(filename)
    # Remove special chars but keep dots and alphanumeric
    filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
    return filename

def get_file_hash(file_path: str) -> str:
    """Calculate SHA256 hash of a file."""
    if not os.path.exists(file_path):
        return ""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in blocks
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def validate_file_type(filename: str, allowed_extensions: list) -> tuple[bool, str]:
    """Validate file extension."""
    if not filename:
        return False, "Filename cannot be empty"
    if '.' not in filename:
        return False, "File has no extension"
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in allowed_extensions:
        return False, f"File extension .{ext} is not allowed"
    return True, "Valid file type"

def validate_mime_type(file_path: str) -> tuple[bool, str]:
    """Basic MIME type validation (stubbed out for simple utility without external deps)."""
    # Real robust MIME checking would use python-magic, but we keep it simple here
    if not os.path.exists(file_path):
        return False, "File does not exist"
    return True, "Valid MIME type"

def get_safe_upload_path(upload_folder: str, filename: str) -> Path:
    """Get a safe pathlib Path for uploaded file."""
    safe_name = sanitize_filename(filename)
    return Path(upload_folder) / safe_name

def cleanup_temp_files(directory: str, max_age_hours=24):
    """Clean up files in directory older than max_age_hours."""
    import time
    if not os.path.exists(directory):
        return
    now = time.time()
    max_age_seconds = max_age_hours * 3600
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if os.path.isfile(file_path):
            if os.stat(file_path).st_mtime < now - max_age_seconds:
                try:
                    os.remove(file_path)
                except Exception:
                    pass

def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = 0
    p = size_bytes
    while p >= 1024 and i < len(size_name) - 1:
        p /= 1024.0
        i += 1
    return f"{p:.2f} {size_name[i]}"
