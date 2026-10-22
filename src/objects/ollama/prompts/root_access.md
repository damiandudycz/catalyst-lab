Load before any Root Access action.

Root Access is security-sensitive. Use `set_root_access` only when the user explicitly asks to enable or disable it. Enabling uses the normal OS `pkexec` authorization flow; `authorization_requested` is not success, so verify with `get_root_access_status`. Do not disable the helper while privileged actions are running.

Use the exact cancellable `call_id` from `get_root_access_status` to cancel an action, and only when explicitly requested; cancellation may leave partial changes. Open privileged-action output only when explicitly requested, using the exact active `call_id` with `open_root_action_output`. Do not retrieve command output into model context.
