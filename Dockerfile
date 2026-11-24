# Base dev box built from https://github.com/marinegor/dotfiles
# Build:  podman build --build-arg UID=$(id -u) \
#           --build-arg DOTFILES_REV=$(git ls-remote https://github.com/marinegor/dotfiles HEAD | cut -f1) \
#           -t devbox-base .
# UID must equal your host uid, otherwise the mounted repo is read-only for 'dev'.
# (don't pass your host gid on macOS: gid 20 collides with Ubuntu's 'dialout')

# ---- rust-tools: compile CLI tools, only /opt/tools/bin is kept ----
FROM ubuntu:latest AS rust-tools

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git pkg-config libssl-dev \
  && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:$PATH \
    CARGO_BUILD_JOBS=2
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path

RUN --mount=type=cache,target=/opt/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/cargo/git,sharing=locked \
    --mount=type=cache,target=/cargo-target,sharing=locked \
    set -e; for crate in bottom du-dust fd-find just ripgrep xq zoxide quickenv hyperfine dysk bat hwatch stylua typstyle httpjail sd; do \
      cargo install --locked --root /opt/tools --target-dir /cargo-target "$crate"; \
    done

RUN --mount=type=cache,target=/opt/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/cargo/git,sharing=locked \
    --mount=type=cache,target=/cargo-target,sharing=locked \
    cargo install --locked --root /opt/tools --target-dir /cargo-target jj-cli

RUN --mount=type=cache,target=/opt/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/cargo/git,sharing=locked \
    --mount=type=cache,target=/cargo-target,sharing=locked \
    cargo install --locked --root /opt/tools --target-dir /cargo-target starship

RUN --mount=type=cache,target=/opt/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/cargo/git,sharing=locked \
    --mount=type=cache,target=/cargo-target,sharing=locked \
    cargo install --locked --root /opt/tools --target-dir /cargo-target typst-cli

RUN --mount=type=cache,target=/opt/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/cargo/git,sharing=locked \
    --mount=type=cache,target=/cargo-target,sharing=locked \
    cargo install --locked --root /opt/tools --target-dir /cargo-target eza

RUN --mount=type=cache,target=/opt/cargo/registry,sharing=locked \
    --mount=type=cache,target=/opt/cargo/git,sharing=locked \
    --mount=type=cache,target=/cargo-target,sharing=locked \
    cargo install --root /opt/tools --target-dir /cargo-target --git https://github.com/asciinema/agg

RUN ln -s eza /opt/tools/bin/exa

# ---- final image ----
FROM ubuntu:latest

ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl wget unzip git git-crypt gh direnv fzf \
      zsh vim neovim openssh-server sudo \
      libffi-dev libgdbm-dev libncurses-dev libnss3-dev libreadline-dev libssl-dev zlib1g-dev \
  && rm -rf /var/lib/apt/lists/*

# Ubuntu images ship a default 'ubuntu' user at uid 1000; replace it with 'dev'
RUN if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi; \
    groupdel ubuntu 2>/dev/null || true; \
    groupadd -g "$GID" dev \
 && useradd -m -u "$UID" -g "$GID" -s /usr/bin/zsh -G sudo dev \
 && echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev \
 && chmod 0440 /etc/sudoers.d/dev

RUN mkdir -p /run/sshd \
 && printf 'PasswordAuthentication no\nPermitRootLogin no\nAllowUsers dev\n' > /etc/ssh/sshd_config.d/10-devbox.conf

# AWS CLI v2 (the PyPI awscli is v1 and has no `aws sso login`)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
 && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscli.zip

COPY --from=rust-tools /opt/tools/bin/ /usr/local/bin/

USER dev
WORKDIR /home/dev

ENV PATH=/home/dev/.local/bin:/home/dev/.pixi/bin:/home/dev/.bun/bin:/home/dev/.opencode/bin:$PATH \
    BUN_INSTALL=/home/dev/.bun \
    EDITOR=vim VISUAL=vim \
    STARSHIP_CONFIG=/home/dev/.config/starship.toml

RUN curl -fsSL https://pixi.sh/install.sh | sh \
 && curl -fsSL https://bun.sh/install | bash \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && rm -rf /home/dev/.cache

RUN uv tool install --python 3.13 glances \
 && for t in pre-commit ruff basedpyright gpustat ty go-task-bin asciinema magic-wormhole; do uv tool install "$t"; done \
 && uv cache clean \
 && rm -rf /home/dev/.cache

# bashrc/zshrc source ~/.cargo/env; rust itself is not in this image
RUN mkdir -p /home/dev/.cargo && : > /home/dev/.cargo/env

# bump DOTFILES_REV to re-clone after pushing dotfiles (see build command at the top)
ARG DOTFILES_REV=main
RUN echo "dotfiles rev: $DOTFILES_REV" \
 && git clone --depth 1 https://github.com/marinegor/dotfiles.git /home/dev/dotfiles \
 && mkdir -p /home/dev/.config \
 && ln -sfn /home/dev/dotfiles/nvim /home/dev/.config/nvim \
 && ln -sf /home/dev/dotfiles/zshrc /home/dev/.zshrc \
 && ln -sf /home/dev/dotfiles/bashrc /home/dev/.bashrc \
 && ln -sf /home/dev/dotfiles/starship.toml /home/dev/.config/starship.toml \
 && ln -sf /home/dev/dotfiles/gitconfig /home/dev/.gitconfig \
 && ln -sf /home/dev/dotfiles/justfile /home/dev/.justfile

RUN nvim --headless "+Lazy! sync" +qa \
 && nvim --headless "+lua for _, p in ipairs({ 'lua', 'javascript', 'typescript', 'python', 'rust', 'bash' }) do if not pcall(vim.treesitter.language.add, p) then vim.cmd('TSInstallSync ' .. p) end end" +qa \
 && rm -rf /home/dev/.cache

RUN curl -fsSL https://opencode.ai/v2/install | bash \
 && opencode --version \
 && rm -rf /home/dev/.cache

USER root
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

# re-runnable gh + AWS SSO login, also run by `localbox create`
COPY localbox-login /usr/local/bin/localbox-login
RUN chmod 755 /usr/local/bin/localbox-login

# sshd sessions don't inherit ENV from the image; pam_env reads /etc/environment
RUN printf '%s\n' \
      'PATH=/home/dev/.local/bin:/home/dev/.pixi/bin:/home/dev/.bun/bin:/home/dev/.opencode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
      'BUN_INSTALL=/home/dev/.bun' \
      'EDITOR=vim' \
      'VISUAL=vim' \
      'STARSHIP_CONFIG=/home/dev/.config/starship.toml' \
      > /etc/environment

# forward AWS/Bedrock credentials from the client's environment (SendEnv on the Mac)
RUN printf 'AcceptEnv AWS_* CLAUDE_CODE_USE_BEDROCK\n' > /etc/ssh/sshd_config.d/20-devbox-env.conf

# ssh sessions get LANG/TERM from the client; force a UTF-8 locale and fall back
# to xterm-256color when the client's terminfo (e.g. xterm-ghostty) isn't installed
RUN printf '%s\n' \
      'export LANG=C.UTF-8 LC_ALL=C.UTF-8' \
      'if [[ -n $TERM ]] && ! infocmp "$TERM" >/dev/null 2>&1; then export TERM=xterm-256color; fi' \
      >> /etc/zsh/zshenv

# dotfiles zshrc/bashrc source this (uv's installer skips it when ~/.local/bin is already on PATH)
RUN printf '%s\n' 'case ":${PATH}:" in *:"$HOME/.local/bin":*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' \
      > /home/dev/.local/bin/env \
 && chown dev:dev /home/dev/.local/bin/env

LABEL localbox.uid=$UID

# GitHub's published host keys (from the TLS-verified API), so git over a forwarded agent never prompts
RUN curl -fsSL --retry 3 https://api.github.com/meta \
 | grep -oE '"(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [^"]+"' | tr -d '"' | sed 's/^/github.com /' \
 > /etc/ssh/ssh_known_hosts \
 && test -s /etc/ssh/ssh_known_hosts

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
