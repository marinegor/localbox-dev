import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import typer
from typer.core import TyperGroup

CONF_INCLUDE = "Include ~/.ssh/localbox.d/*.conf"

# variables sent to the box over ssh (SendEnv); the image's sshd must AcceptEnv them.
# AWS_PROFILE is deliberately absent: the box has no ~/.aws, a profile name would break it.
AWS_FORWARD = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK",
)


# profile logged into with `aws sso login` inside the box on create
AWS_SSO_PROFILE = os.environ.get("LOCALBOX_AWS_PROFILE", "security-1-bedrock-models")


OPENCODE_CONFIG = Path(
    os.environ.get(
        "LOCALBOX_OPENCODE_CONFIG", Path.home() / ".config" / "opencode" / "opencode.json"
    )
)


def die(msg: str) -> "None":
    print(f"localbox: {msg}", file=sys.stderr)
    sys.exit(1)


class Box:
    def __init__(self) -> None:
        self.image = os.environ.get("LOCALBOX_IMAGE", "devbox-base")
        self.pubkey = Path(
            os.environ.get("LOCALBOX_PUBKEY", Path.home() / ".ssh" / "id_ed25519.pub")
        )
        self.ssh_dir = Path.home() / ".ssh"
        self.conf_dir = self.ssh_dir / "localbox.d"
        self.cwd = Path.cwd()
        repo = re.sub(r"[^A-Za-z0-9._-]", "-", self.cwd.name)
        self.name = f"{repo}-localbox"
        self.mount_dir = f"/home/dev/{repo}"
        self.conf_path = self.conf_dir / f"{self.name}.conf"

    def podman(
        self, *args: str, check: bool = True, input: "str | None" = None
    ) -> subprocess.CompletedProcess:
        try:
            r = subprocess.run(
                ["podman", *args], capture_output=True, text=True, input=input
            )
        except FileNotFoundError:
            die("podman not found on PATH")
        if check and r.returncode != 0:
            die(r.stderr.strip() or f"podman {' '.join(args)} failed")
        return r

    def exists(self) -> bool:
        return self.podman("container", "exists", self.name, check=False).returncode == 0

    def running(self) -> bool:
        out = self.podman("inspect", "-f", "{{.State.Running}}", self.name).stdout
        return out.strip() == "true"

    def host_port(self) -> str:
        out = self.podman("port", self.name, "22/tcp").stdout
        return out.splitlines()[0].rsplit(":", 1)[1].strip()

    def mount_inode(self) -> str:
        return str(os.stat(self.cwd).st_ino)

    def check_mount(self) -> None:
        # a bind mount follows the folder's inode, not its path: if the folder was
        # moved and re-created (e.g. re-cloned) the box keeps showing the old one
        fmt = '{{index .Config.Labels "localbox.inode"}}'
        recorded = self.podman("inspect", "-f", fmt, self.name).stdout.strip()
        if recorded and recorded != self.mount_inode():
            die(
                f"the folder at {self.cwd} was replaced after {self.name} was created,\n"
                f"so the box still mounts the old one (e.g. a moved or re-cloned repo).\n"
                f"recreate it: localbox delete && localbox create"
            )

    def ensure_include(self) -> None:
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)
        cfg = self.ssh_dir / "config"
        current = cfg.read_text() if cfg.exists() else ""
        if CONF_INCLUDE not in current:
            cfg.write_text(f"{CONF_INCLUDE}\n{current}")
            cfg.chmod(0o600)

    def write_ssh_conf(self, port: str) -> None:
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        self.conf_path.write_text(
            f"Host {self.name}\n"
            f"  HostName 127.0.0.1\n"
            f"  Port {port}\n"
            f"  User dev\n"
            f"  ForwardAgent yes\n"
            f"  AddKeysToAgent yes\n"
            f"  RequestTTY yes\n"
            f"  SendEnv {' '.join(AWS_FORWARD)}\n"
            f"  RemoteCommand cd {self.mount_dir} && exec zsh -l\n"
            f"  UserKnownHostsFile /dev/null\n"
            f"  StrictHostKeyChecking no\n"
            f"  LogLevel ERROR\n"
        )

    def wait_ssh(self) -> None:
        # RemoteCommand=none: ssh refuses a command-line command otherwise
        probe = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", "RemoteCommand=none", "-o", "RequestTTY=no",
            self.name, "true",
        ]
        for _ in range(30):
            if subprocess.run(probe, capture_output=True).returncode == 0:
                return
            time.sleep(1)
        die(f"sshd in {self.name} did not become ready")

    @staticmethod
    def aws(*args: str) -> "subprocess.CompletedProcess | None":
        try:
            return subprocess.run(
                ["aws", *args], capture_output=True, text=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def load_aws_env(self) -> None:
        """Put AWS credentials into our own environment so `ssh` forwards them.

        Never fatal: connecting must work without AWS. Secrets stay in memory and
        in the ssh environment; they are not put on a command line or written to disk.
        """
        if os.environ.get("LOCALBOX_NO_AWS"):
            return

        # already exported (static keys, bearer token): forward as they are
        if not (
            os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        ):
            r = self.aws("configure", "export-credentials", "--format", "env-no-export")
            if r is None or r.returncode != 0:
                if r is not None:
                    print(
                        "localbox: AWS credentials not forwarded "
                        "(expired SSO? run `aws sso login`; LOCALBOX_NO_AWS=1 silences this)",
                        file=sys.stderr,
                    )
                return
            for line in r.stdout.splitlines():
                key, _, value = line.removeprefix("export ").partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key in AWS_FORWARD and value:
                    os.environ[key] = value

        if not (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")):
            r = self.aws("configure", "get", "region")
            if r is not None and r.returncode == 0 and r.stdout.strip():
                os.environ["AWS_REGION"] = r.stdout.strip()

    @staticmethod
    def host_aws_profile_config(profile: str) -> str:
        """The `[profile <name>]` section (and its sso-session, if any) of the host's aws config."""
        cfg = Path(os.environ.get("AWS_CONFIG_FILE", Path.home() / ".aws" / "config"))
        if not cfg.is_file():
            return ""
        sections: "dict[str, list[str]]" = {}
        current = None
        for line in cfg.read_text().splitlines():
            m = re.match(r"\s*\[(.+?)\]\s*$", line)
            if m:
                current = " ".join(m.group(1).split())
                sections[current] = [line.strip()]
            elif current is not None:
                sections[current].append(line.rstrip())

        wanted = [f"profile {profile}"]
        for line in sections.get(wanted[0], []):
            key, _, value = line.partition("=")
            if key.strip() == "sso_session":
                wanted.append(f"sso-session {value.strip()}")
        return "".join("\n".join(sections[w]).rstrip() + "\n\n" for w in wanted if w in sections)

    def copy_opencode_config(self) -> None:
        """Copy the host's opencode.json into the box (Bedrock region, profile and model list).

        Never fatal: the box is already created and usable without it.
        """
        if os.environ.get("LOCALBOX_NO_AWS"):
            return
        if not OPENCODE_CONFIG.is_file():
            print(
                f"localbox: skipping opencode config copy ({OPENCODE_CONFIG} not found; "
                f"set LOCALBOX_OPENCODE_CONFIG)",
                file=sys.stderr,
            )
            return
        r = self.podman(
            "exec", "-i", "-u", "dev", self.name,
            "sh", "-c", "mkdir -p ~/.config/opencode && cat > ~/.config/opencode/opencode.json",
            input=OPENCODE_CONFIG.read_text(),
            check=False,
        )
        if r.returncode != 0:
            print(f"localbox: opencode config copy failed: {r.stderr.strip()}", file=sys.stderr)

    def write_aws_config(self) -> None:
        """Copy the host's SSO profile section into the box so `localbox-login` can use it."""
        config = self.host_aws_profile_config(AWS_SSO_PROFILE)
        if not config:
            print(
                f"localbox: profile {AWS_SSO_PROFILE} not found in ~/.aws/config; "
                f"AWS login will be skipped (set LOCALBOX_AWS_PROFILE)",
                file=sys.stderr,
            )
            return
        self.podman(
            "exec", "-i", "-u", "dev", self.name,
            "sh", "-c", "mkdir -p ~/.aws && cat > ~/.aws/config",
            input=config,
            check=False,
        )

    def login(self) -> None:
        """Run `localbox-login` (gh + AWS SSO, device-code flows) inside the box.

        Never fatal: the box is already created and usable without it.
        """
        if os.environ.get("LOCALBOX_NO_LOGIN"):
            return
        no_aws = bool(os.environ.get("LOCALBOX_NO_AWS"))
        if not no_aws:
            self.write_aws_config()

        print(f"logging in inside {self.name}; open the URLs below on this machine")
        r = subprocess.run(
            [
                "podman", "exec", "-it" if sys.stdin.isatty() else "-i", "-u", "dev", self.name,
                "localbox-login", "gh" if no_aws else "all",
            ]
        )
        if r.returncode != 0:
            print(
                f"localbox: login did not complete; retry inside the box with `localbox-login` "
                f"(an image without it needs a rebuild)",
                file=sys.stderr,
            )

    def check_uid(self) -> None:
        fmt = '{{index .Config.Labels "localbox.uid"}}'
        r = self.podman("image", "inspect", "-f", fmt, self.image, check=False)
        if r.returncode != 0:
            die(f"image {self.image} not found (build it first)")
        built = r.stdout.strip()
        if built and built != str(os.getuid()):
            die(
                f"{self.image} was built for uid {built}, you are uid {os.getuid()}: "
                f"the mounted folder would be read-only.\n"
                f"rebuild with: podman build --build-arg UID=$(id -u) -t {self.image} ."
            )

    def create(self) -> None:
        if self.exists():
            die(f"{self.name} already exists (use: localbox connect)")
        if not self.pubkey.is_file():
            die(f"public key not found: {self.pubkey} (set LOCALBOX_PUBKEY)")

        self.check_uid()
        self.podman(
            "run", "-d",
            "--name", self.name,
            "--label", "localbox=true",
            "--label", f"localbox.inode={self.mount_inode()}",
            "-p", "127.0.0.1::22",
            "-e", f"LOCALBOX_AWS_PROFILE={AWS_SSO_PROFILE}",
            "-e", f"SSH_PUBKEY={self.pubkey.read_text().strip()}",
            "-v", f"{self.cwd}:{self.mount_dir}",
            self.image,
        )
        self.ensure_include()
        self.write_ssh_conf(self.host_port())
        self.wait_ssh()
        print(f"created {self.name} (mounted {self.cwd} at {self.mount_dir})")
        self.copy_opencode_config()
        self.login()
        print("connect with: localbox connect")

    def connect(self) -> None:
        if not self.exists():
            die(f"{self.name} does not exist (run: localbox create)")
        self.check_mount()
        if not self.running():
            self.podman("start", self.name)

        self.ensure_include()
        self.write_ssh_conf(self.host_port())
        self.wait_ssh()
        self.load_aws_env()
        # a user-level `SendEnv AWS_*` would otherwise leak the profile name
        for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            os.environ.pop(var, None)
        os.execvp("ssh", ["ssh", self.name])

    def display(self) -> None:
        if not self.exists():
            die(f"{self.name} does not exist (run: localbox create)")
        self.check_mount()
        print(f"box:     {self.name} ({'running' if self.running() else 'stopped'})")
        print(f"mounted: {self.cwd} -> {self.mount_dir}")
        if not self.running():
            print("ssh:     not available while stopped; `localbox connect` starts it")
            return

        port = self.host_port()
        remote = shlex.quote(f"cd {self.mount_dir} && exec zsh -l")
        print(f"ssh:     ssh {self.name}")
        print(
            "direct:  ssh -t -p {port} -o UserKnownHostsFile=/dev/null "
            "-o StrictHostKeyChecking=no -o LogLevel=ERROR dev@127.0.0.1 {remote}".format(
                port=port, remote=remote
            )
        )
        conf = self.conf_path.read_text() if self.conf_path.exists() else ""
        if f"Port {port}\n" not in conf:
            print("note:    ssh config is missing or has an old port; `localbox connect` refreshes it")

    def delete(self) -> None:
        if not self.exists():
            die(f"{self.name} does not exist")
        answer = input(f"Delete {self.name}? Files in {self.cwd} are kept. [y/N] ")
        if answer.strip().lower() != "y":
            die("aborted")

        self.podman("rm", "-f", self.name)
        self.conf_path.unlink(missing_ok=True)
        print(f"deleted {self.name} (files in {self.cwd} kept)")


class PrefixGroup(TyperGroup):
    """Accept any unambiguous prefix of a subcommand, e.g. `cr` for `create`."""

    def get_command(self, ctx: Any, cmd_name: str):
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        matches = [n for n in self.list_commands(ctx) if n.startswith(cmd_name)]
        if len(matches) > 1:
            ctx.fail(f"ambiguous command '{cmd_name}': could be {', '.join(sorted(matches))}")
        return super().get_command(ctx, matches[0]) if matches else None

    def resolve_command(self, ctx: Any, args: "list[str]"):
        _, cmd, rest = super().resolve_command(ctx, args)
        return (cmd.name if cmd else None), cmd, rest


app = typer.Typer(
    cls=PrefixGroup,
    help=(
        "Per-repo rootless Podman dev box you can ssh into.\n\n"
        "Run from inside a repository. The box is named <repo>-localbox and mounts "
        "the current directory at /home/dev/<repo>. "
        "Subcommands can be abbreviated to any unambiguous prefix (cr, co, di, de)."
    ),
    epilog=(
        "Environment:\n\n"
        "LOCALBOX_IMAGE: image to run (default: devbox-base)\n\n"
        "LOCALBOX_PUBKEY: public key to authorize (default: ~/.ssh/id_ed25519.pub)\n\n"
        "LOCALBOX_OPENCODE_CONFIG: opencode.json copied into the box on create (default: ~/.config/opencode/opencode.json)\n\n"
        "LOCALBOX_AWS_PROFILE: SSO profile to log into on create (default: security-1-bedrock-models)\n\n"
        "LOCALBOX_NO_AWS: set to 1 to skip AWS login and the opencode.json copy on create, and credential forwarding on connect\n\n"
        "LOCALBOX_NO_LOGIN: set to 1 to skip `localbox-login` (gh and AWS) on create"
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)


@app.command()
def create() -> None:
    """Start a new box, copy your opencode.json into it and run `localbox-login` (gh + AWS SSO) there."""
    Box().create()


@app.command()
def connect() -> None:
    """Start (if stopped) and ssh into the box.

    Forwards AWS/Bedrock credentials from your shell (SSO ones via
    `aws configure export-credentials`) as environment variables; nothing is copied.
    """
    Box().connect()


@app.command()
def display() -> None:
    """Show the ssh command for this box (changes nothing)."""
    Box().display()


@app.command()
def delete() -> None:
    """Remove the box; the host directory is kept."""
    Box().delete()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
