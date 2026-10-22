Use `get_settings` to inspect current app preferences, Git identity, the Library location, and the Ollama server URL. Credentials, query strings, and fragments in that URL are redacted. Use `get_root_access_status` to see whether the Linux root helper is active and which privileged action names are running; it does not return command output. `library_location` is read-only here; change it with `change_library_location`. `update_settings` accepts one exact `setting` and a `value`:

| Settings area | `setting` | Value |
| --- | --- | --- |
| Navigation | `navigation_style` | `modal` or `push` |
| Build queue | `pause_queue_on_failure` | Boolean |
| Root Access | `keep_root_unlocked` | Boolean; controls whether authorized root access stays unlocked after an action |
| QEMU | `qemu_cpus` | Integer from 1 to 64 |
| QEMU | `qemu_memory_gib` | Integer from 1 to 256 |
| Snapshots | `auto_clean_snapshots` | Boolean; enabling may immediately delete unused snapshots and asks for confirmation |
| Default toolset | `default_toolset_id` | Exact ID from `list_options` category `library_toolsets`, or `default` to restore automatic selection |
| Default runner | `default_runner_id` | Exact value from `list_options` category `runners`; `default` restores automatic app selection and `host` selects Host |
| Project stage layout | `project_stages_view_mode` | `compact`, `full`, or `list` |
| Git identity | `git_identity_mode` | `system` or `custom`; names and email are separate settings |
| Git identity | `git_user_name`, `git_user_email` | User-provided text |
| Ollama | `ollama_url` | User-provided server URL |
| Ollama | `ollama_model` | Exact installed model name from `list_ollama_models`; affects future chats only |

Use `list_options` before setting a selectable ID. For `default_toolset_id`, request `library_toolsets`, which matches the Settings screen and includes all installed toolsets; `default` restores automatic selection. For the AI Assistance “Check connection” action, call `list_ollama_models`; it contacts the configured server and returns connection failures or installed models. Change `ollama_url` only to an endpoint supplied by the user, then call `list_ollama_models` again to verify it. Verify model names there before setting `ollama_model`. Use `open_ollama_model_catalog` only when the user explicitly asks to browse the “Browse all models” link. Download a model with `pull_ollama_model` only when explicitly requested. To switch Git identity to `custom`, first collect the exact name and email from the user, update both fields, and set `git_identity_mode` to `custom`; to return to the system identity, set the mode to `system`. Do not invent preference names or values. Never expose URL credentials or private data. Changing the Library location belongs to `change_library_location`, not `update_settings`.
