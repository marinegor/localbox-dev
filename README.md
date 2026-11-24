# localbox-dev

A small, per-repository rootless Podman dev box you can SSH into. Run it from inside any repo and you get a disposable Ubuntu container with your usual tools, while only that repo folder is mounted read-write on your Mac.

## What it is

- `devbox-base` — a Podman image based on `ubuntu:latest` with Rust CLI tools, `uv`, `pixi`, `bun`, `opencode`, Neovim + plugins, and more.
- `localbox` — a tiny command that creates, connects to, and deletes per-repo containers from that image.

Each box is named `<repo>-localbox` and mounts `$PWD` at `/home/dev/<repo>` inside the container. Nothing else from your Mac is visible.

## Requirements

- macOS with [Podman](https://podman.io/) installed and a working Podman machine.
- SSH key at `~/.ssh/id_ed25519.pub` (or set `LOCALBOX_PUBKEY`).
- [uv](https://docs.astral.sh/uv/) to install the CLI.
- For AWS/Bedrock: [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) on the Mac, with an SSO profile in `~/.aws/config`.

## Install

```sh
uv tool install /path/to/this/repo/localbox-cli
```

## Build the image

Run this once, from the repo root:

```sh
podman build --build-arg UID=$(id -u) -t devbox-base .
```

`UID` must match your macOS user id, otherwise the mounted folder is read-only for the container user.

## Usage

From inside any project:

```sh
localbox create    # start a box for this repo
localbox connect   # ssh into it (starts it if stopped)
localbox display   # show the ssh command without changing anything
localbox delete    # remove the box; the host folder is kept
```

Subcommands can be abbreviated to any unambiguous prefix: `localbox cr`, `localbox co`, `localbox di`, `localbox de`.

Once connected, you land in `/home/dev/<repo>` with zsh.

## Logins: `localbox-login`

The image ships `localbox-login`. `localbox create` runs it once; run it again inside the box whenever a login expires.

```sh
localbox-login            # gh + AWS SSO (skips logins that are still valid)
localbox-login gh         # only GitHub
localbox-login aws        # only AWS SSO
localbox-login --force    # log in again even if still valid
```

Both use device-code flows: open the printed URL on the Mac and enter the code. Failures are never fatal; the box works without them.

## AWS / Bedrock

- `create` copies the `[profile security-1-bedrock-models]` section of your `~/.aws/config` into the box and passes the profile name to it as `LOCALBOX_AWS_PROFILE`. Set `LOCALBOX_AWS_PROFILE` on the Mac to use another profile.
- `create` also copies your `~/.config/opencode/opencode.json` to the same path in the box (region, profile and model list for Bedrock). Set `LOCALBOX_OPENCODE_CONFIG` to copy another file.
- `connect` forwards short-lived credentials from your shell (`aws configure export-credentials`) as environment variables. Nothing is written to disk.
- `LOCALBOX_NO_AWS=1` skips the AWS login, the config copy and the credential forwarding. `LOCALBOX_NO_LOGIN=1` skips `localbox-login` on create entirely.
- `aws sso login` needs AWS CLI **v2** in the image (the PyPI `awscli` is v1 and has no `sso` login).

## SSH / Git

The generated SSH config forwards your macOS SSH agent, so git operations over SSH and `gh` via SSH work without copying keys into the image. GitHub's host keys are baked in, so the first connection is prompt-free.

## Why a stale-mount check

Podman bind mounts follow the folder's inode, not its path. If you move the repo aside and re-clone it at the same path, an existing box would still show the old folder. `localbox connect` and `localbox display` detect this and tell you to recreate the box.

## Image size

The final image is roughly 2 GB. The largest layers are the apt packages, the Python tools installed by `uv`, the Rust CLI binaries, and `opencode`.
