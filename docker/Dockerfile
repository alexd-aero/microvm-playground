# The playground userland. Deliberately mirrors the Debian guest that the QEMU
# and Firecracker backends boot, so the shell feels identical whichever backend
# is in use.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color \
    COLORTERM=truecolor \
    PAGER=less \
    LESS=-R

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils util-linux procps psmisc \
        git curl wget ca-certificates openssh-client rsync \
        vim nano less tree file jq unzip zip tar gzip \
        htop tmux neofetch \
        python3 \
        iputils-ping dnsutils net-tools iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Shell environment. No mvmp_resize() here: a container attach is a real pty, so
# SIGWINCH propagates properly and the size looks after itself.
RUN printf '%s\n' \
    "export LANG=C.UTF-8" \
    "export LC_ALL=C.UTF-8" \
    "export TERM=xterm-256color" \
    "export COLORTERM=truecolor" \
    "export PS1='\[\e[1;38;5;48m\]\u@\h\[\e[0m\]:\[\e[1;38;5;75m\]\w\[\e[0m\]# '" \
    "alias ls='ls --color=auto'" \
    "alias grep='grep --color=auto'" \
    "alias ll='ls -alF --color=auto'" \
    "[ -f /etc/motd ] && cat /etc/motd" \
    > /etc/profile.d/00-mvmp.sh \
 && chmod 0644 /etc/profile.d/00-mvmp.sh

# Real ESC bytes: /etc/motd is displayed with cat, which does not interpret
# escape sequences.
RUN { \
      printf '\n'; \
      printf '  \033[38;5;51m╭────────────────────────────────────────────────╮\033[0m\n'; \
      printf '  \033[38;5;51m│\033[0m\033[1;97m   microvm playground · container · disposable  \033[0m\033[38;5;51m│\033[0m\n'; \
      printf '  \033[38;5;51m╰────────────────────────────────────────────────╯\033[0m\n'; \
      printf '\n'; \
      printf '   \033[90mDebian 12 · UTF-8 · truecolor · full outbound internet\033[0m\n'; \
      printf '   \033[33m⚠\033[0m  \033[90mShared kernel: fast and native, weaker isolation than a VM.\033[0m\n'; \
      printf '   \033[33m⚠\033[0m  \033[90mThis playground is destroyed when you close it.\033[0m\n'; \
      printf '\n'; \
    } > /etc/motd

WORKDIR /root
CMD ["sleep", "infinity"]
