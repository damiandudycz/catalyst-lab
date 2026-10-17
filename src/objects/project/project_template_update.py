"""Updating projects from templates they were generated from, and from Git repositories they were cloned from.

Project created from template has project-template.json (template repository, its commit, selected options and ids of
stages created from template stages). Commits that generate project from template ("Create project from template",
"Update from template") usually change this file, the newest of them is base of update.

Update compares three versions: base, project (its current commit, changes have to be saved first) and new version
(project generated again from latest template, or latest commit of remote branch). Arguments of stages (stage.json)
are compared one by one, other files as whole. Stages are matched by ids, so renamed stages are compared too. Changes
made only in new version are applied, changes made only in project are kept, and changes made in both differently are
decided by user (keep project version or use new one).

History of project follows new version: commits of user made since base are replayed on top of it (new commit with
generated template, or remote commit), using the same comparison and decisions of user. So history of cloned project
stays history of its repository with user commits on top, and template project has user commits on top of the latest
generated template. Without user commits, project is just moved to new version.
"""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile, uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from .project_template import (
    APP_REPOSITORY_URL, ProjectTemplate, TemplateError, app_template_tree, apply_project_template,
    download_app_template, validate_template_repository, _run_git
)
from .git_directory import GitDirectory

TEMPLATE_STATE_FILE = "project-template.json"
TEMPLATE_STATE_FORMAT = 1

# ------------------------------------------------------------------------------
# State of project created from template.
# ------------------------------------------------------------------------------

@dataclass
class TemplateState:
    repository_url: str
    repository_path: str # Directory of template in repository.
    template_name: str
    commit: str | None # Commit of template repository used to generate project.
    selected: dict[str, Any] # Values of options.
    stage_ids: dict[str, str] # Ids of project stages by ids of template stages.
    template_tree: str | None = None # Git tree of template directory, to check if template changed.
    source_kind: str = "repository" # "app" for official templates stored in Catalyst Lab.

    @classmethod
    def load(cls, project_path: str) -> TemplateState | None:
        try:
            with open(os.path.join(project_path, TEMPLATE_STATE_FILE), encoding="utf-8") as file:
                data = json.load(file)
            if data["format"] != TEMPLATE_STATE_FORMAT:
                return None
            repository_url = data["repository_url"]
            repository_path = data["repository_path"]
            source_kind = data["source_kind"]
            return cls(repository_url=repository_url, repository_path=repository_path,
                       template_name=data["template_name"], commit=data["commit"], selected=data["selected"],
                       stage_ids=data["stage_ids"], template_tree=data["template_tree"], source_kind=source_kind)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, project_path: str):
        data = {
            "format": TEMPLATE_STATE_FORMAT,
            "repository_url": self.repository_url,
            "repository_path": self.repository_path,
            "template_name": self.template_name,
            "commit": self.commit,
            "selected": self.selected,
            "stage_ids": self.stage_ids,
            "template_tree": self.template_tree,
            "source_kind": self.source_kind,
        }
        with open(os.path.join(project_path, TEMPLATE_STATE_FILE), "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)
            file.write("\n")

    @property
    def uuid_stage_ids(self) -> dict[str, uuid.UUID]:
        return {key: uuid.UUID(value) for key, value in self.stage_ids.items()}

def record_template_state(project_path: str, template: ProjectTemplate, selected: dict[str, Any], commit: str | None,
                          stage_ids: dict[str, uuid.UUID], template_tree: str | None = None) -> TemplateState:
    """Saves state of project generated from template (committed with generated files)."""
    state = TemplateState(repository_url=template.repository_url or (APP_REPOSITORY_URL if template.source_kind == "app" else ""),
                          repository_path=template.repository_path,
                          template_name=template.name, commit=commit, selected=selected,
                          stage_ids={key: str(value) for key, value in stage_ids.items()}, template_tree=template_tree,
                          source_kind=template.source_kind)
    state.save(project_path)
    return state

def repository_commit(path: str) -> str | None:
    result = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None

def template_tree(repository: str, repository_path: str) -> str | None:
    """Git tree of template directory in latest commit of repository (changes when template changes)."""
    result = subprocess.run(["git", "-C", repository, "rev-parse", f"HEAD:{repository_path}" if repository_path else "HEAD^{tree}"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None

def template_update_available(project_directory) -> bool | None:
    """Check whether the source template changed. In-app templates compare only their own directory tree; external
    templates also compare regenerated project output. None when it can't be checked (eg. offline)."""
    project_path = project_directory.directory_path()
    state = TemplateState.load(project_path)
    if state is None or not state.repository_url:
        return None
    if state.source_kind == "app":
        try:
            latest_tree = app_template_tree(state.repository_path)
        except Exception as e:
            print(f"Failed to check updates of app template: {e}")
            return None
        # App code changes and other templates don't affect this project's template update state.
        return latest_tree is None or latest_tree != state.template_tree
    with tempfile.TemporaryDirectory(prefix="template-check-") as temporary:
        repository = os.path.join(temporary, "repository")
        try:
            _run_git(["git", "clone", "--depth", "1", "--quiet", state.repository_url, repository], timeout=60)
        except Exception as e:
            print(f"Failed to check updates of template: {e}")
            return None
        if state.template_tree:
            if template_tree(repository, state.repository_path) != state.template_tree:
                return True
        elif repository_commit(repository) != state.commit:
            return True
        template = ProjectTemplate.load(os.path.join(repository, state.repository_path),
                                        repository_url=state.repository_url, repository_path=state.repository_path)
        # Same template, generated again and compared with last generated version.
        base = template_base_commit(project_path)
        if base is None:
            return None
        try:
            generated = os.path.join(temporary, "template")
            os.makedirs(generated)
            apply_project_template(_RenderTarget(project_directory, generated), template,
                                   template.resolve(state.selected, project_name=project_directory.name),
                                   log=lambda message: None, stage_ids=state.uuid_stage_ids)
            return _read_commit(project_path, base, os.path.join(temporary, "base")).entries != read_project_version(generated).entries
        except Exception as e:
            print(f"Failed to generate template to check updates: {e}")
            return None

# ------------------------------------------------------------------------------
# Comparing versions.
# ------------------------------------------------------------------------------

_STAGES_DIRECTORY = "stages"
_STAGE_FILE = "stage.json"
_IGNORED = {".git", TEMPLATE_STATE_FILE, ".DS_Store"}

@dataclass
class ProjectVersion:
    """Files of project version by keys: "path" for files outside of stages, "stage:<id>/path" for files of stages,
    "stage:<id>#<argument>" for arguments of stages (values in stage.json format)."""
    entries: dict[str, Any] = field(default_factory=dict)
    stage_directories: dict[str, str] = field(default_factory=dict) # Directory names of stages by their ids.
    stage_names: dict[str, str] = field(default_factory=dict)

def read_project_version(root: str | None) -> ProjectVersion:
    version = ProjectVersion()
    if root is None or not os.path.isdir(root):
        return version
    for directory, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in _IGNORED]
        relative_directory = os.path.relpath(directory, root)
        parts = [] if relative_directory == "." else relative_directory.split(os.sep)
        if len(parts) == 2 and parts[0] == _STAGES_DIRECTORY and _STAGE_FILE in files:
            # Stage directory: its id is in stage.json.
            try:
                with open(os.path.join(directory, _STAGE_FILE), encoding="utf-8") as file:
                    stage = json.load(file)
                stage_id = stage["id"]["value"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            version.stage_directories[stage_id] = parts[1]
            version.stage_names[stage_id] = (stage.get("name") or {}).get("value", parts[1])
            for key, value in stage.items():
                version.entries[f"stage:{stage_id}#{key}"] = value
            for stage_directory, stage_subdirectories, stage_files in os.walk(directory):
                stage_subdirectories[:] = [name for name in stage_subdirectories if name not in _IGNORED]
                for name in stage_files:
                    path = os.path.join(stage_directory, name)
                    relative = os.path.relpath(path, directory)
                    if relative == _STAGE_FILE or name in _IGNORED or os.path.islink(path):
                        continue
                    with open(path, "rb") as file:
                        version.entries[f"stage:{stage_id}/{relative}"] = file.read()
            directories[:] = [] # Files of stage were read.
            continue
        if parts and parts[0] == _STAGES_DIRECTORY:
            continue # Directory of stage without stage.json, or stages directory itself.
        for name in files:
            path = os.path.join(directory, name)
            if name in _IGNORED or os.path.islink(path):
                continue
            with open(path, "rb") as file:
                version.entries[os.path.relpath(path, root)] = file.read()
    return version

_MISSING = object()

@dataclass
class TemplateChange:
    key: str
    title: str # Stage and argument or path, for user.
    base: Any
    project: Any
    template: Any
    conflict: bool # Changed in project and in template differently, user decides.
    use_template: bool # Decision, template version is used (automatic changes always use it).
    parts: list[TemplateChange] = field(default_factory=list) # Changes grouped in this one (eg. files of new stage).

    def flattened(self) -> list[TemplateChange]:
        """Changes grouped in this one, with its decision (group is decided as whole)."""
        if not self.parts:
            return [self]
        for part in self.parts:
            part.use_template = self.use_template
        return [part for change in self.parts for part in change.flattened()]

def describe_value(value) -> str:
    if value is _MISSING or value is None:
        return "Not set"
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return f"Binary file ({len(value)} bytes)"
        lines = text.splitlines()
        return (lines[0] + (f" … (+{len(lines) - 1} lines)" if len(lines) > 1 else "")) if lines else "Empty file"
    if isinstance(value, dict) and "value" in value:
        inner = value["value"]
        if isinstance(inner, list):
            return ", ".join(describe_value(item) for item in inner) or "Empty list"
        if isinstance(inner, dict):
            return str(inner.get("path") or inner)
        return str(inner)
    return str(value)

def compare_versions(base: ProjectVersion, project: ProjectVersion, template: ProjectVersion) -> list[TemplateChange]:
    """Changes of template to apply to project: made only in template (automatic) or made differently in project and
    template (conflicts)."""
    names = {**base.stage_names, **template.stage_names, **project.stage_names}
    changes = []
    for key in sorted(set(base.entries) | set(project.entries) | set(template.entries)):
        base_value = base.entries.get(key, _MISSING)
        project_value = project.entries.get(key, _MISSING)
        template_value = template.entries.get(key, _MISSING)
        if project_value == template_value or base_value == template_value:
            continue # Same in both, or template didn't change it (project version is kept).
        if key.startswith("stage:"):
            body = key[len("stage:"):]
            stage_id, _, rest = body.partition("#") if "#" in body else body.partition("/")
            title = f"{names.get(stage_id, stage_id)}: {rest}"
        else:
            title = key
        conflict = project_value != base_value
        changes.append(TemplateChange(key=key, title=title, base=base_value, project=project_value,
                                      template=template_value, conflict=conflict, use_template=not conflict))
    return _group_stage_changes(changes, base, project, template)

def _is_stage_key(key: str) -> bool:
    """Key of whole stage (change of stage added or removed)."""
    return key.startswith("stage:") and "/" not in key and "#" not in key

def _stage_id_of(key: str) -> str | None:
    if not key.startswith("stage:"):
        return None
    body = key[len("stage:"):]
    return body.partition("#")[0] if "#" in body else body.partition("/")[0]

def _group_stage_changes(changes: list[TemplateChange], base: ProjectVersion, project: ProjectVersion,
                         template: ProjectVersion) -> list[TemplateChange]:
    """Changes of stages added to project (only in template) or removed from it (not in template anymore) are shown as
    one change. Removed stage changed in project (settings, or files added to it) is conflict decided as whole: stage
    is kept as it is, or removed completely."""
    grouped, by_stage = [], {}
    for change in changes:
        stage_id = _stage_id_of(change.key)
        added = stage_id is not None and stage_id not in project.stage_directories and stage_id in template.stage_directories
        removed = stage_id is not None and stage_id in project.stage_directories and stage_id not in template.stage_directories
        if removed or (added and not change.conflict):
            by_stage.setdefault((stage_id, added), []).append(change)
        else:
            grouped.append(change)
    for (stage_id, added), parts in by_stage.items():
        if added:
            name = template.stage_names.get(stage_id, stage_id)
            grouped.append(TemplateChange(key=f"stage:{stage_id}", title=f"New stage {name}", base=_MISSING,
                                          project=_MISSING, template="Stage", conflict=False, use_template=True, parts=parts))
            continue
        name = project.stage_names.get(stage_id, stage_id)
        # Files added to stage in project (not generated): removed with stage when template version is used.
        known = {part.key for part in parts}
        for key, value in project.entries.items():
            if _stage_id_of(key) == stage_id and key not in known and base.entries.get(key, _MISSING) != value:
                parts.append(TemplateChange(key=key, title=key, base=base.entries.get(key, _MISSING), project=value,
                                            template=_MISSING, conflict=True, use_template=False))
        changed = any(part.conflict for part in parts)
        if changed:
            grouped.append(TemplateChange(key=f"stage:{stage_id}", title=f"{name} is removed by template, but changed in project",
                                          base="Stage", project="Stage with changes", template="Removed",
                                          conflict=True, use_template=False, parts=parts))
        else:
            grouped.append(TemplateChange(key=f"stage:{stage_id}", title=f"Removed stage {name}", base="Stage",
                                          project="Stage", template=_MISSING, conflict=False, use_template=True, parts=parts))
    return grouped

def apply_changes(project_path: str, changes: list[TemplateChange], project: ProjectVersion, template: ProjectVersion):
    """Writes template versions of changes that use them to project."""
    def stage_directory(stage_id: str) -> str:
        name = project.stage_directories.get(stage_id) or template.stage_directories.get(stage_id) or stage_id
        return os.path.join(project_path, _STAGES_DIRECTORY, name)
    stage_files: dict[str, dict] = {}
    for change in [part for change in changes if change.use_template for part in change.flattened()]:
        if change.key.startswith("stage:") and "#" in change.key:
            stage_id, _, argument = change.key[len("stage:"):].partition("#")
            if stage_id not in stage_files:
                path = os.path.join(stage_directory(stage_id), _STAGE_FILE)
                try:
                    with open(path, encoding="utf-8") as file:
                        stage_files[stage_id] = json.load(file)
                except (OSError, ValueError):
                    stage_files[stage_id] = {}
            if change.template is _MISSING:
                stage_files[stage_id].pop(argument, None)
            else:
                stage_files[stage_id][argument] = change.template
            continue
        if change.key.startswith("stage:"):
            stage_id, _, relative = change.key[len("stage:"):].partition("/")
            path = os.path.join(stage_directory(stage_id), relative)
        else:
            path = os.path.join(project_path, change.key)
        if change.template is _MISSING:
            if os.path.isfile(path):
                os.remove(path)
        else:
            # File replaced by directory in template (eg. package.license as directory of files) is removed first.
            parent = os.path.dirname(path)
            while parent.startswith(project_path) and parent != project_path:
                if os.path.isfile(parent):
                    os.remove(parent)
                    break
                parent = os.path.dirname(parent)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as file:
                file.write(change.template)
    for stage_id, stage in stage_files.items():
        directory = stage_directory(stage_id)
        if not stage:
            # Stage removed from template.
            if os.path.isfile(os.path.join(directory, _STAGE_FILE)):
                os.remove(os.path.join(directory, _STAGE_FILE))
            continue
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, _STAGE_FILE), "w", encoding="utf-8") as file:
            json.dump(stage, file, indent=4)
        # Directory follows name of stage (renamed in template).
        name = (stage.get("name") or {}).get("value")
        if name:
            from .git_directory import GitDirectory
            renamed = os.path.join(project_path, _STAGES_DIRECTORY, GitDirectory.sanitized_name_for_name(name))
            if renamed != directory and not os.path.exists(renamed):
                os.rename(directory, renamed)
    # Directories of removed stages and files.
    for directory, directories, files in os.walk(os.path.join(project_path, _STAGES_DIRECTORY), topdown=False):
        if os.path.abspath(directory) == os.path.abspath(os.path.join(project_path, _STAGES_DIRECTORY)):
            continue
        # os.walk's child list still includes directories removed earlier in this
        # bottom-up traversal. Check the filesystem so empty parents disappear too.
        with os.scandir(directory) as remaining:
            empty = next(remaining, None) is None
        if empty:
            os.rmdir(directory)

# ------------------------------------------------------------------------------
# Git helpers.
# ------------------------------------------------------------------------------

def _git(path: str, *arguments, check: bool = True, environment: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", path, *arguments], capture_output=True, text=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(environment or {})})
    if check and result.returncode != 0:
        raise TemplateError(f"git {arguments[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()

def has_unsaved_changes(project_path: str) -> bool:
    return bool(_git(project_path, "status", "--porcelain", check=False))

def _extract_commit(repository_path: str, commit: str, destination: str) -> bool:
    """Files of commit written to destination directory."""
    archive = subprocess.run(["git", "-C", repository_path, "archive", commit], capture_output=True)
    if archive.returncode != 0:
        return False
    extract = subprocess.run(["tar", "-x", "-C", destination], input=archive.stdout, capture_output=True)
    return extract.returncode == 0

def _commit_directory(repository_path: str, directory: str, message: str, parents: list[str],
                      author: dict[str, str] | None = None) -> str:
    """Creates commit with files of directory and given parents in repository of project, without changing its
    branch or files. Returns its hash."""
    with tempfile.TemporaryDirectory() as temporary:
        environment = {"GIT_INDEX_FILE": os.path.join(temporary, "index"), **(author or {})}
        git_directory = os.path.join(repository_path, ".git")
        def git(*arguments) -> str:
            return _git(repository_path, f"--git-dir={git_directory}", f"--work-tree={directory}", *arguments,
                        environment=environment)
        git("add", "--all", "--force", ".")
        tree = git("write-tree")
        return git("commit-tree", tree, *[argument for parent in parents for argument in ("-p", parent)], "-m", message)

def _commit_author(repository_path: str, commit: str) -> dict[str, str]:
    name, email, date = _git(repository_path, "log", "-1", "--format=%an%x00%ae%x00%aI", commit).split("\0")
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}

def _read_commit(repository_path: str, commit: str, directory: str) -> ProjectVersion:
    os.makedirs(directory)
    if not _extract_commit(repository_path, commit, directory):
        raise TemplateError(f"Failed to read commit {commit[:8]}")
    return read_project_version(directory)

def rebase_project(repository_path: str, onto: str, base: str, changes: list[TemplateChange], temporary: str,
                   log: Callable[[str], None]) -> str:
    """Replays commits made since base on top of commit onto, and moves branch to them. Changes of every commit are
    applied with the same comparison as update: settings and files changed by commit are taken, unless new version
    changed them too, then decision of user is used (keep project version, or use new version). Returns new HEAD."""
    if _git(repository_path, "rev-list", "--merges", f"{base}..HEAD"):
        raise TemplateError("Project history has merge commits since last update, they can't be replayed")
    commits = _git(repository_path, "rev-list", "--reverse", f"{base}..HEAD").split()
    # Decisions about changes made in project and new version, by keys: True when new version is used.
    decisions = {part.key: part.use_template for change in changes if change.conflict for part in change.flattened()}
    decisions.update({change.key: change.use_template for change in changes if change.conflict and change.parts})
    current = os.path.join(temporary, "rebase")
    _read_commit(repository_path, onto, current)
    # Stages removed by new version, but changed in project: kept stages are restored as they were at base (commits
    # change them then), commits don't recreate removed ones.
    removed_stages = [change for change in changes if change.conflict and change.parts and _is_stage_key(change.key)]
    removed_ids = {_stage_id_of(change.key) for change in removed_stages if change.use_template}
    kept_ids = {_stage_id_of(change.key) for change in removed_stages if not change.use_template}
    if kept_ids:
        base_directory = os.path.join(temporary, "rebase-base")
        base_version = _read_commit(repository_path, base, base_directory)
        for stage_id in kept_ids:
            if name := base_version.stage_directories.get(stage_id):
                target = os.path.join(current, _STAGES_DIRECTORY, name)
                if not os.path.exists(target):
                    shutil.copytree(os.path.join(base_directory, _STAGES_DIRECTORY, name), target)
    head = onto
    for index, commit in enumerate(commits):
        parent_version = _read_commit(repository_path, f"{commit}^", os.path.join(temporary, f"parent-{index}"))
        commit_version = _read_commit(repository_path, commit, os.path.join(temporary, f"commit-{index}"))
        current_version = read_project_version(current)
        commit_changes = compare_versions(parent_version, current_version, commit_version)
        for change in commit_changes:
            if _stage_id_of(change.key) in removed_ids:
                change.use_template = False # Stage was removed by new version, commit doesn't recreate it.
            elif change.conflict:
                # Commit changed what new version changed too: version chosen by user (commit is project version).
                # Grouped changes are decided as whole, their parts get decision of group.
                change.use_template = not decisions.get(change.key, False)
                for part in change.parts:
                    if part.conflict:
                        part.use_template = not decisions.get(part.key, False)
        apply_changes(current, commit_changes, current_version, commit_version)
        message = _git(repository_path, "log", "-1", "--format=%B", commit)
        title = message.splitlines()[0] if message else commit[:8]
        replayed = _commit_directory(repository_path, current, message, [head], author=_commit_author(repository_path, commit))
        if _git(repository_path, "rev-parse", f"{replayed}^{{tree}}") == _git(repository_path, "rev-parse", f"{head}^{{tree}}"):
            log(f"Skipped {title}, its changes are not used") # Eg. changes of stage removed by new version.
            continue
        head = replayed
        log(f"Replayed {title}")
    old_head = _git(repository_path, "rev-parse", "HEAD")
    _git(repository_path, "update-ref", "-m", "Catalyst Lab: update", "HEAD", head, old_head)
    _git(repository_path, "reset", "--hard", "--quiet", head)
    return head

# ------------------------------------------------------------------------------
# Update.
# ------------------------------------------------------------------------------

@dataclass
class ProjectUpdate:
    """Update prepared for project: changes of new version, commit of new version and base of project."""
    source: str # "template" or "repository".
    onto: str # Commit of new version.
    base: str # Last commit of the previously generated template version.
    changes: list[TemplateChange]
    temporary_directory: str
    fast_forward: bool # Project has no own commits since base, it's moved to new version.
    architecture: Any = None # Architecture set by template (it can change with options).
    # Project is moved to new version also when it has own commits (they are discarded, they stay in Git reflog).
    drop_project_changes: bool = False

    def cleanup(self):
        shutil.rmtree(self.temporary_directory, ignore_errors=True)

    def apply(self, project_directory, log: Callable[[str], None] = print):
        project_path = project_directory.directory_path()
        if has_unsaved_changes(project_path):
            raise TemplateError("Project has not saved changes, save or discard them first")
        if self.fast_forward or self.drop_project_changes:
            old_head = _git(project_path, "rev-parse", "HEAD")
            message = "Catalyst Lab: update, project changes dropped" if self.drop_project_changes else "Catalyst Lab: update"
            _git(project_path, "update-ref", "-m", message, "HEAD", self.onto, old_head)
            _git(project_path, "reset", "--hard", "--quiet", self.onto)
        else:
            rebase_project(project_path, self.onto, self.base, self.changes, self.temporary_directory, log)
        if self.architecture is not None and self.architecture != project_directory.get_architecture():
            from .repository import Repository
            project_directory.initialize_metadata().architecture = self.architecture
            Repository.ProjectDirectory.save()
        if hasattr(project_directory, "_stages"):
            del project_directory._stages # Stages are read again.
        from .git_directory import GitDirectoryEvent
        project_directory.event_bus.emit(GitDirectoryEvent.CONTENT_CHANGED, project_directory)
        project_directory.update_logs()

def _new_temporary_directory(prefix: str) -> str:
    from .repository import Repository, resolve_path
    root = resolve_path(Repository.Settings.value.temporary_location)
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=root)

def _require_saved(project_path: str):
    if not GitDirectory.is_git_directory(project_path):
        raise TemplateError("Project isn't Git repository")
    if has_unsaved_changes(project_path):
        raise TemplateError("Project has not saved changes, save or discard them first")

class _RenderTarget:
    """Project generated into other directory (template version), with configuration of real project."""
    def __init__(self, project_directory, path: str):
        self._project = project_directory
        self._path = path
        class _NoEvents:
            def emit(self, *arguments):
                pass
            def subscribe(self, *arguments, **keywords):
                pass
        self.event_bus = _NoEvents()
    def directory_path(self) -> str:
        return self._path
    def stage_directory_path(self, name: str) -> str:
        return os.path.join(self._path, _STAGES_DIRECTORY, self._project.sanitized_name_for_name(name))
    @property
    def stages(self):
        # Created stages, cleared by apply_project_template.
        if "_stages" not in self.__dict__:
            self.__dict__["_stages"] = []
        return self.__dict__["_stages"]
    def __getattr__(self, name):
        # Configuration of real project (toolset, releng...), its private state isn't shared.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._project, name)

# Messages of commits generating project from template.
_TEMPLATE_COMMIT_MESSAGES = ("Create project from template ", "Update from template ") # Start of messages.

def template_base_commit(project_path: str) -> str | None:
    """Newest commit generating project from template. It changes project-template.json, unless only files generated
    from the same template and options changed (eg. new version of app generates stages differently), so commits are
    also found by their messages."""
    by_state = _git(project_path, "log", "-1", "--format=%H", "--", TEMPLATE_STATE_FILE, check=False) or None
    by_message = _git(project_path, "log", "-1", "--format=%H", "--extended-regexp",
                      "--grep", "^(" + "|".join(_TEMPLATE_COMMIT_MESSAGES) + ")", check=False) or None
    commits = [commit for commit in (by_state, by_message) if commit]
    if len(commits) < 2 or by_state == by_message:
        return by_state or by_message
    # Newer of them (the other one is its ancestor).
    is_ancestor = subprocess.run(["git", "-C", project_path, "merge-base", "--is-ancestor", by_state, by_message],
                                 capture_output=True).returncode == 0
    return by_message if is_ancestor else by_state

def prepare_template_update(project_directory, log: Callable[[str], None],
                            selected: dict[str, Any] | None = None, force: bool = False) -> ProjectUpdate | None:
    """Downloads latest version of template, generates project from it (as commit on top of base) and compares it with
    project. Options are the ones selected before, or new selected values. None when template didn't change project,
    unless forced (clean version of template is prepared to drop changes of project)."""
    project_path = project_directory.directory_path()
    state = TemplateState.load(project_path)
    if state is None:
        raise TemplateError("Project wasn't created from template")
    _require_saved(project_path)
    base = template_base_commit(project_path)
    if base is None:
        raise TemplateError(f"{TEMPLATE_STATE_FILE} isn't saved in project history")
    temporary = _new_temporary_directory("template-update-")
    try:
        if state.source_kind == "app":
            log(f"Downloading template {state.template_name} from Catalyst Lab")
            template, _ = download_app_template(state.repository_path, temporary_parent=temporary)
            repository = None
        else:
            repository = os.path.join(temporary, "repository")
            log(f"Downloading {state.repository_url}")
            _run_git(["git", "clone", "--depth", "1", "--quiet", state.repository_url, repository])
            if state.repository_path not in validate_template_repository(repository, check_sizes=True):
                raise TemplateError(f"Template {state.repository_path or state.template_name} is not in repository anymore")
            template = ProjectTemplate.load(os.path.join(repository, state.repository_path),
                                            repository_url=state.repository_url, repository_path=state.repository_path)
        generated = os.path.join(temporary, "template")
        os.makedirs(generated)
        log(f"Generating project from {template.name}")
        stage_ids = apply_project_template(_RenderTarget(project_directory, generated), template,
                                           template.resolve(selected if selected is not None else state.selected,
                                                            project_name=project_directory.name),
                                           log=log, stage_ids=state.uuid_stage_ids)
        base_version = _read_commit(project_path, base, os.path.join(temporary, "base"))
        changes = compare_versions(base_version, read_project_version(project_path), read_project_version(generated))
        commit = repository_commit(repository) if repository else None
        options_changed = selected is not None and selected != state.selected
        new_tree = template.source_tree if state.source_kind == "app" else template_tree(repository, state.repository_path)
        source_unchanged = new_tree == state.template_tree if state.source_kind == "app" else commit == state.commit
        if not changes and source_unchanged and not options_changed and not force:
            shutil.rmtree(temporary, ignore_errors=True)
            return None
        names = template.resolve(selected if selected is not None else state.selected, project_name=project_directory.name)
        architecture = template.generate(names).architecture
        # New version of template, as commit on top of base.
        if selected is not None:
            state.selected = selected
        record_template_state(generated, template, state.selected, commit, stage_ids, new_tree)
        onto = _commit_directory(project_path, generated, f"Update from template {template.name}", [base])
        if _git(project_path, "rev-parse", f"{onto}^{{tree}}") == _git(project_path, "rev-parse", f"{base}^{{tree}}"):
            onto = base # Template didn't change (prepared to drop changes of project), project returns to base.
        head = _git(project_path, "rev-parse", "HEAD")
        return ProjectUpdate(source="template", onto=onto, base=base, changes=changes, temporary_directory=temporary,
                             fast_forward=head == base, architecture=architecture)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

@dataclass
class RepositoryOrigin:
    url: str
    branch: str | None

def repository_origin(project_path: str) -> RepositoryOrigin | None:
    """Remote repository of project cloned from Git repository (not created from template)."""
    if TemplateState.load(project_path) is not None or not GitDirectory.is_git_directory(project_path):
        return None
    url = _git(project_path, "remote", "get-url", "origin", check=False)
    if not url:
        return None
    branch = _git(project_path, "symbolic-ref", "--short", "HEAD", check=False) or None
    return RepositoryOrigin(url=url, branch=branch)

def prepare_repository_update(project_directory, log: Callable[[str], None], force: bool = False) -> ProjectUpdate | None:
    """Downloads changes of remote branch and compares them with project. None when there is nothing new, unless
    forced (remote version is prepared to drop changes of project)."""
    project_path = project_directory.directory_path()
    origin = repository_origin(project_path)
    if origin is None:
        raise TemplateError("Project wasn't cloned from Git repository")
    _require_saved(project_path)
    log(f"Fetching {origin.url}")
    _git(project_path, "fetch", "--quiet", "origin")
    upstream = _git(project_path, "rev-parse", "@{upstream}", check=False) or _git(project_path, "rev-parse", "origin/HEAD")
    head = _git(project_path, "rev-parse", "HEAD")
    base = _git(project_path, "merge-base", "HEAD", upstream)
    if base == upstream and not (force and head != upstream):
        return None # Remote has nothing new (or project is the same as remote).
    temporary = _new_temporary_directory("repository-update-")
    try:
        changes = compare_versions(_read_commit(project_path, base, os.path.join(temporary, "base")),
                                   read_project_version(project_path),
                                   _read_commit(project_path, upstream, os.path.join(temporary, "remote")))
        return ProjectUpdate(source="repository", onto=upstream, base=base, changes=changes,
                             temporary_directory=temporary, fast_forward=head == base)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

def own_commits_count(project_path: str) -> int:
    """Commits of project that are not in its template (made since last generation) or remote branch."""
    if TemplateState.load(project_path) is not None:
        base = template_base_commit(project_path)
    else:
        base = _git(project_path, "rev-parse", "@{upstream}", check=False)
    if not base:
        return 0
    # Commits not changing files (eg. made by restoring clean template) are not own changes.
    count = _git(project_path, "rev-list", "--count", f"{base}..HEAD", "--", ".", check=False)
    return int(count) if count.isdigit() else 0

def project_overlays(project_directory) -> list:
    """Overlays used by stages of project (repos argument)."""
    from .repository import Repository
    overlay_ids = set()
    for stage in project_directory.stages:
        value = getattr(stage, "repos", None)
        for item in value if isinstance(value, list) else []:
            if isinstance(item, uuid.UUID):
                overlay_ids.add(item)
    return [overlay for overlay in Repository.OverlayDirectory.value if overlay.id in overlay_ids]
