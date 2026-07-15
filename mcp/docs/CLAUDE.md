# CLAUDE.md

This project's agent working guide is maintained in `AGENTS.md`, the cross-tool
standard that Codex and other agents read automatically. Claude Code does not
read `AGENTS.md` directly, so this file imports it:

@AGENTS.md

Keep BRIER workflow guidance in `AGENTS.md` (single source of truth); this shim
only forwards it to Claude Code. Add Claude-Code-specific notes below this line
if you ever need any; otherwise leave it as the import above.
