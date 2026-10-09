# 06 BACKUP ISOLATION AND RESTORE

- **Isolation:** Backups are stored in `__JULES_BACKUP`. While the FUSE layer hides this from `readdir`, it is physically located on the same disk and accessible directly.
- **Restore:** No functionality implemented.

**Status:** FAIL
