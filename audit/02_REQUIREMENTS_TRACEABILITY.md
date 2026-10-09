# 02 REQUIREMENTS TRACEABILITY MATRIX

| Req ID | Requirement | File | Status | Notes |
|---|---|---|---|---|
| 1 | Real cloud-backed storage path | None | FAIL | Operates only on local POSIX filesystem. |
| 2 | Controlled Google Jules integration | None | FAIL | No API integration exists. |
| 3 | Dedicated `JULES_ZONE` | `gateway.py` (L: 90) | FAIL | Enforced in FUSE, but underlying directory is exposed. |
| 4 | No unauthorized modification outside zone | `gateway.py` (L: 94) | FAIL | See #3. |
| 5 | Backup before destructive changes | `gateway.py` (L: 52) | FAIL | Implemented locally, but lacks real recovery tools. |
| 6 | Backup isolation | `gateway.py` (L: 17) | FAIL | Stored in `__JULES_BACKUP` on the same disk. Not isolated. |
| 7 | Actual deduplication | `gateway.py` (L: 64) | FAIL | Basic SHA256 copy; lacks reference counting/GC. |
| 8 | Version recovery and restore | None | FAIL | No mechanism to restore files. |
| 9 | Windows mount persistence | None | FAIL | Linux `fusepy` foreground script. |
| 10 | Credentials handling | None | FAIL | No cloud credentials used. |
| 11 | Audit logs and error handling | `gateway.py` (L: 38) | FAIL | Basic SQLite logging, but no real security boundary. |
