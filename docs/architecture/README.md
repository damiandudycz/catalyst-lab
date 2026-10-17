# Architecture references

- [Core classes and extension points](base-classes.md) — shared persistence, event, Git-directory, operation, and main-section foundations.
- [Build execution and virtual machines](build-execution.md) — toolsets, Lima, OverlayFS sessions, caches, and platform-specific build paths.
- Project templates keep the safe expression evaluator in `src/objects/project/project_template_expressions.py`; parsing, generation, repository handling, and application remain in `project_template.py`.
- VM image creation and custom-kernel preparation live with the VM runtime in `src/objects/qemu/qemu_run.py`; `qemu_run_view.py` handles the interactive screen.
