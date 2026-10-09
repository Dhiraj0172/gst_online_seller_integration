# 03 ARCHITECTURE AND DATA FLOW

## Request Flow
`Local File Operation -> FUSE (gateway.py) -> SQLite (metadata) / local copy (backup) -> Local Filesystem (root)`

## Diagram
N/A - The system does not connect to any external services.

## Conclusion
The architecture is a local FUSE wrapper, not a cloud gateway.
