---
name: localbox
description: Work with localbox, the per-repo rootless Podman dev box (create/connect/display/delete, rebuild the devbox-base image, GitHub and AWS Bedrock logins via localbox-login, troubleshooting). Use when the user mentions localbox, devbox-base, the "box", a *-localbox container, or logging into gh/AWS/Bedrock inside it.
---

# localbox

`localbox` creates one disposable Ubuntu container per repository (`<repo>-localbox`, image `devbox-base`) that you ssh into. Only the repo folder is mounted (read-write at `/home/dev/<repo>`); nothing else from the Mac is visible. Source: this repo.

## Layout

| Path | What |
| ---- | ---- |
| `localbox-cli/src/localbox/cli.py` | the CLI (Python, Typer), installed with `uv tool install --force ./localbox-cli` |
| `Dockerfile` | the `devbox-base` image (AWS CLI v2, gh, opencode, sshd, ...) |
| `localbox-login` | script baked into the image at `/usr/local/bin/localbox-login` |
| `entrypoint.sh` | authorizes the ssh key and starts sshd |

Reinstall the CLI after editing `cli.py`; the image after editing the `Dockerfile` or `localbox-login`.

## Host commands

Run from inside a repository. Subcommands accept any unambiguous prefix (`cr`, `co`, `di`, `de`; `c` is ambiguous).

- `localbox create` - start the box, copy `~/.config/opencode/opencode.json` into it, copy the SSO profile section of `~/.aws/config`, then run `localbox-login` interactively.
- `localbox connect` - start if stopped, refresh the ssh config, forward AWS credentials (`aws configure export-credentials`) as env vars, ssh in.
- `localbox display` - show box state and the ssh command; changes nothing.
- `localbox delete` - remove the box (asks first); the host folder is kept.

Environment: `LOCALBOX_IMAGE`, `LOCALBOX_PUBKEY`, `LOCALBOX_AWS_PROFILE` (default `security-1-bedrock-models`), `LOCALBOX_OPENCODE_CONFIG`, `LOCALBOX_NO_AWS=1` (skip AWS login, config copy, credential forwarding), `LOCALBOX_NO_LOGIN=1` (skip `localbox-login` on create).

## Inside the box: `localbox-login`

```sh
localbox-login [-f|--force] [gh|aws|all]
```

Logs into GitHub (`gh auth login`, git over ssh) and AWS SSO (`aws sso login --no-browser --use-device-code` for `$LOCALBOX_AWS_PROFILE`). Already valid logins are skipped unless `--force`. Both flows print a URL and code: open the URL on the Mac. Tokens live only in that box and expire; re-run the command.

## Build the image

```sh
podman build --build-arg UID=$(id -u) \
  --build-arg DOTFILES_REV=$(git ls-remote https://github.com/marinegor/dotfiles HEAD | cut -f1) \
  -t devbox-base .
```

`UID` must equal the host uid or the mounted repo is read-only. Existing boxes keep their old image: `localbox delete && localbox create` to pick up a rebuild. The build takes minutes: run it in the background and do not poll.

## Gotchas

- The image entrypoint starts sshd. To run a one-off command in the image, override it: `podman run --rm --entrypoint sh devbox-base -c '...'`; otherwise the command hangs.
- `aws sso login` exists only in AWS CLI **v2**. The PyPI `awscli` is v1; the image installs v2 from the official zip.
- OpenCode does not read `region` from an AWS profile. Without `region` in `opencode.json` or `AWS_REGION` it uses `us-east-1`, and the role is only allowed in `eu-central-1`: `not authorized to perform: bedrock:InvokeModelWithResponseStream ... us-east-1`. The copied `opencode.json` sets the region.
- A user-level `Host *-localbox` / `SendEnv AWS_*` block in `~/.ssh/config` leaks `AWS_PROFILE` into plain `ssh` sessions (the box has the profile, but env credentials plus a missing profile break the credential chain). `connect` pops `AWS_PROFILE` for its own ssh.
- Env credentials forwarded by `connect` reach only shells started that way and expire (about 12 h); a long-lived `opencode serve` in the box keeps the stale env. Prefer `localbox-login` and the `profile` in `opencode.json`.
- Bind mounts follow the folder's inode. If the repo was moved or re-cloned, `connect` says so: `localbox delete && localbox create`.
- `podman` must be running (`podman machine start`). "Cannot connect to Podman" means the machine is stopped.
- Boxes are named from the current directory's basename, so run `localbox` from the repo root.

## Debugging

```sh
podman ps -a --format '{{.Names}} {{.Status}}'
podman exec -u dev <repo>-localbox <command>            # inspect a box without ssh
ssh -o RemoteCommand=none -o RequestTTY=no <repo>-localbox '<command>'
grep -i bedrock ~/.local/share/opencode/log/opencode.log   # inside the box
```

Never print credentials: filter `env` output by name only (`sed -E 's/=.*/=<set>/'`).
