"""Git author identity used by commits created by Catalyst Lab."""
from __future__ import annotations
from dataclasses import dataclass
import os
import subprocess


@dataclass(frozen=True)
class GitIdentity:
    name: str = ""
    email: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.name.strip() and self.email.strip())

    def missing_description(self) -> str:
        missing = []
        if not self.name.strip():
            missing.append("name")
        if not self.email.strip():
            missing.append("email")
        return " and ".join(missing)


def system_git_identity(repository_path: str | None = None) -> GitIdentity:
    """Read the normal Git config stack (system/global, and local when a repo is given)."""
    values = []
    for key in ("user.name", "user.email"):
        command = ["git"]
        if repository_path:
            command.extend(("-C", repository_path))
        command.extend(("config", "--get", key))
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            values.append("")
            continue
        values.append(result.stdout.strip() if result.returncode == 0 else "")
    return GitIdentity(*values)


def effective_git_identity(repository_path: str | None = None) -> GitIdentity:
    """Return the identity selected in app settings, or the host Git identity."""
    from .repositories import Repository

    settings = Repository.Settings.value
    if settings.git_identity_mode == "custom":
        return GitIdentity(settings.git_user_name, settings.git_user_email)
    return system_git_identity(repository_path)


def require_git_identity(repository_path: str | None = None) -> GitIdentity:
    identity = effective_git_identity(repository_path)
    if not identity.configured:
        missing = identity.missing_description()
        raise RuntimeError(
            f"Git {missing} is not configured. Set it in Catalyst Lab Settings under Git identity, "
            "or configure `git config --global user.name` and `user.email`."
        )
    return identity


def git_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Apply app-specific identity to a Git subprocess without changing system Git config."""
    env = dict(os.environ if environment is None else environment)
    from .repositories import Repository

    settings = Repository.Settings.value
    if settings.git_identity_mode == "custom":
        identity = GitIdentity(settings.git_user_name, settings.git_user_email)
        if identity.configured:
            env.update({
                "GIT_AUTHOR_NAME": identity.name,
                "GIT_AUTHOR_EMAIL": identity.email,
                "GIT_COMMITTER_NAME": identity.name,
                "GIT_COMMITTER_EMAIL": identity.email,
            })
    return env
