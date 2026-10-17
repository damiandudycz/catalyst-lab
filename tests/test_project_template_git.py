"""Git operations used by project templates don't prompt for credentials."""
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import project_template
from catalystlab.project_template import (
    TemplateError, TemplateOverlayReference, _replace_overlay_references, _run_git, load_cloned_template,
)
from catalystlab import project_template_update


class ProjectTemplateGitTests(unittest.TestCase):
    def test_overlay_references_are_replaced_recursively(self):
        overlay_id = object()
        value = {"repos": [{"overlay": TemplateOverlayReference("gentoo-overlay")},
                           [TemplateOverlayReference("gentoo-overlay")]]}

        replaced = _replace_overlay_references(value, {"gentoo-overlay": overlay_id})

        self.assertEqual(replaced, {"repos": [{"overlay": overlay_id}, [overlay_id]]})

    def test_failed_repository_copy_removes_partial_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = types.SimpleNamespace(value=types.SimpleNamespace(temporary_location=temporary))

            def partial_copy(source, destination, **kwargs):
                Path(destination).mkdir()
                raise OSError("copy failed")

            with patch("catalystlab.repository.Repository.Settings", settings, create=True), \
                 patch.object(project_template, "validate_template_repository", return_value=[""]), \
                 patch.object(project_template.shutil, "copytree", side_effect=partial_copy):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    load_cloned_template("/project", "https://example.test/template.git", "")

            self.assertEqual(list(Path(temporary).iterdir()), [])

    @patch('catalystlab.project_template.subprocess.run')
    def test_returns_bytes_and_disables_credential_prompts(self, run):
        run.return_value = types.SimpleNamespace(returncode=0, stdout=b'template')

        self.assertEqual(_run_git(['git', 'show', 'HEAD:template.toml']), b'template')
        self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    @patch('catalystlab.project_template.subprocess.run')
    def test_reports_last_line_of_git_failure(self, run):
        run.return_value = types.SimpleNamespace(returncode=1, stdout=b'fatal: first line\nfatal: detail\n')

        with self.assertRaisesRegex(TemplateError, 'fatal: detail'):
            _run_git(['git', 'show', 'HEAD:missing'])

    @patch.dict('os.environ', {'CATALYSTLAB_TEMPLATE_BRANCH': 'codex/template-test'})
    @patch('catalystlab.project_template._run_git')
    def test_app_template_checkout_uses_development_branch(self, run_git):
        project_template._app_repository_checkout('/temporary/repository')

        command = run_git.call_args.args[0]
        self.assertEqual(command[command.index('--branch') + 1], 'codex/template-test')

    def test_project_template_update_accepts_linked_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / '.git').write_text('gitdir: /somewhere/.git/worktrees/project')
            with patch.object(project_template_update, 'has_unsaved_changes', return_value=False):
                project_template_update._require_saved(temporary)


if __name__ == '__main__':
    unittest.main()
