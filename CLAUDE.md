@AGENTS.md

# Claude Code notes
- The shared rules, commands, layout and contract are in `AGENTS.md` (imported above). Follow them.
- Before editing, read `docs/STATUS.md`; after a work session, update it (Done / In progress / Unverified / Handoff log).
- This sandbox has CPU-only `pycolmap`: use `--no-dense` or `PipelineConfig(dense=False, backend="pycolmap")` here.
  Dense and Poisson stages can only be verified on the user's GTX 1650 PC. Say so rather than guessing.
- Pass `python -I` when running anything that reads untrusted files, and keep new scripts outside data folders.
- When you add or change a public behaviour, add a test in `tests/` and keep `python -m pytest` green (and `--runslow` before big changes to the pipeline).
- The user writes in English, sometimes tersely; confirm the interpretation of unclear one-liners in your reply
  instead of guessing silently.
