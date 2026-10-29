# Source provenance

Imported on 2026-10-29 from the latest local release, copilot-compat-windows-1.1.0.

Release source: the local copilot-compat-windows-1.1.0 distribution.

Development source: the corresponding distribution working directory. Machine-specific absolute paths are omitted from this repository.

The initial import preserved release files byte-for-byte. Version 1.1.1 adds optional credential-helper hostname overrides, bridge restart commands, and an in-place updater with backup and rollback. SHA256SUMS.txt describes the updated distributable files, including restart.cmd and update.cmd. Windows command launchers use CRLF line endings.

Version 1.1.2 supports reinstalling into recognized uninstalled directories while preserving backups, and displays the resolved installation directory.

The existing test_distribution.py was copied from the development directory and extended for hostname and restart behavior. Its installation tests write disposable fixtures to the sibling distribution-tests directory. Run `python -m unittest -v test_distribution` to verify the updated sources.

This supersedes the earlier 1.0.0 import. Python 3.11 or newer is required; see README.md for usage.
