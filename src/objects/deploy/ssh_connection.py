from __future__ import annotations
import os, shlex, stat, subprocess, threading, time, uuid

# ------------------------------------------------------------------------------
# SSH connection to machine where build is deployed (usually booted from Gentoo LiveCD).
# Uses ssh of this computer with shared master connection: password is used once when connecting (passed to ssh
# through SSH_ASKPASS from environment of ssh process, never stored), next commands reuse authenticated connection.
# Host keys are not checked, LiveCD generates new ones on every boot.

class SSHConnection:
    is_local = False

    def __init__(self, host: str, port: int = 22, user: str = "root"):
        self.host = host
        self.port = port
        self.user = user
        # Short path in /tmp, sockets paths are limited to about 100 characters.
        self.control_path = os.path.join("/tmp", f"catalystlab-ssh-{uuid.uuid4().hex[:12]}")
        self.connected = False

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def _command(self, *arguments: str, master: bool = False) -> list[str]:
        options = {
            "ControlPath": self.control_path,
            "StrictHostKeyChecking": "no",
            "UserKnownHostsFile": "/dev/null",
            "LogLevel": "ERROR",
            "ServerAliveInterval": "15",
            "ConnectTimeout": "15",
        }
        if master:
            options |= {"ControlMaster": "yes", "ControlPersist": "no"}
        else:
            options |= {"ControlMaster": "no", "BatchMode": "yes"}
        command = ["ssh", "-p", str(self.port)]
        for key, value in options.items():
            command += ["-o", f"{key}={value}"]
        return command + [self.destination, *arguments]

    # Connection:

    def connect(self, password: str | None) -> str | None:
        """Opens master connection. Returns error message, or None when connected. Master runs as child process of
        app (not backgrounded by ssh itself), it's ready when its control socket exists."""
        environment = dict(os.environ)
        askpass = None
        options = []
        if password:
            # Script printing password from environment of ssh process.
            askpass = os.path.join("/tmp", f"catalystlab-askpass-{uuid.uuid4().hex[:12]}")
            with open(askpass, "w", encoding="utf-8") as file:
                file.write('#!/bin/sh\nprintf "%s\\n" "$CATALYSTLAB_SSH_PASSWORD"\n')
            os.chmod(askpass, stat.S_IRWXU)
            environment |= {"SSH_ASKPASS": askpass, "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": environment.get("DISPLAY", ":0"),
                            "CATALYSTLAB_SSH_PASSWORD": password}
            options = ["-o", "NumberOfPasswordPrompts=1", "-o", "PreferredAuthentications=keyboard-interactive,password,publickey"]
        command = self._command(master=True)
        command = command[:-1] + options + ["-N", command[-1]]
        try:
            self._master = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self._master.poll() is not None:
                    message = self._master.stderr.read().strip().splitlines()
                    return message[-1] if message else f"Failed to connect to {self.host}"
                if os.path.exists(self.control_path) and self._check():
                    self.connected = True
                    return None
                time.sleep(0.2)
            self._master.terminate()
            return f"Connection to {self.host} timed out"
        finally:
            if askpass and os.path.exists(askpass):
                os.remove(askpass)

    def _check(self) -> bool:
        result = subprocess.run(["ssh", "-o", f"ControlPath={self.control_path}", "-O", "check", self.destination],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        return result.returncode == 0

    def close(self):
        if not self.connected:
            return
        subprocess.run(["ssh", "-o", f"ControlPath={self.control_path}", "-O", "exit", self.destination],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        master = getattr(self, "_master", None)
        if master and master.poll() is None:
            master.terminate()
        self.connected = False

    def is_alive(self) -> bool:
        return self.connected and self._check()

    # Commands:

    def start(self, script: str) -> subprocess.Popen:
        """Starts bash script on machine, output (with errors) is in stdout of returned process."""
        process = subprocess.Popen(self._command("bash -s"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1, start_new_session=True)
        process.stdin.write(script)
        process.stdin.close()
        return process

    def run(self, script: str, output_handler=print, process_holder: list | None = None) -> bool:
        """Runs bash script on machine, output goes to handler line by line."""
        process = self.start(script)
        if process_holder is not None:
            process_holder.append(process)
        for line in process.stdout:
            output_handler(line.rstrip("\n"))
        return process.wait() == 0

    def output(self, script: str) -> str:
        """Output of script, raises when it fails."""
        lines = []
        if not self.run(script, lines.append):
            raise RuntimeError(lines[-1] if lines else "Command failed on remote machine")
        return "\n".join(lines)

    def stream_file(self, path: str, command: str, output_handler=print, progress_handler=None,
                    process_holder: list | None = None) -> bool:
        """Sends file to stdin of command on machine (eg. tar extracting it). Progress handler gets fraction of sent
        data."""
        process = subprocess.Popen(self._command(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        if process_holder is not None:
            process_holder.append(process)
        def read_output():
            for line in process.stdout:
                output_handler(line.decode(errors="replace").rstrip("\n"))
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        size = os.path.getsize(path) or 1
        sent = 0
        reported = -1
        try:
            with open(path, "rb") as file:
                while chunk := file.read(1024 * 1024):
                    process.stdin.write(chunk)
                    sent += len(chunk)
                    percent = sent * 100 // size
                    if progress_handler and percent != reported:
                        reported = percent
                        progress_handler(sent / size)
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass # Command ended (failed or cancelled), its exit code is reported.
        code = process.wait()
        reader.join(timeout=10)
        return code == 0

def quote(value: str) -> str:
    return shlex.quote(value)
