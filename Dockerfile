# muse-openclaw-shim: the Muse Gadget SDK service + an OpenAI-compatible shim.
#
# Runtime only: pairing happens OUTSIDE this image with ./pair.sh, which
# produces a bundle; the container takes it as MUSE_PAIRING_B64 (or
# MUSE_PAIRING_FILE) and unpacks it into tmpfs. See README.md / DESIGN.md.
#
#   docker build -t muse-openclaw-shim .
FROM debian:bookworm-slim

ARG MUSEGADGET_SDK_REPO=https://github.com/facebookincubator/muse-gadget-sdk
ARG MUSEGADGET_SDK_REF=55cbf4822976592002ee05579fca5eb1f79c676b
ARG UV_VERSION=0.9.9

ENV DEBIAN_FRONTEND=noninteractive

# dbus/gi/cryptography MUST come from the distro (the venv uses
# --system-site-packages to see them). No bluez/dbus daemon: this image never
# pairs. util-linux (setpriv) is part of the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-dbus python3-gi python3-cryptography \
      curl ca-certificates git tini \
 && rm -rf /var/lib/apt/lists/*

# uv, pinned the way the SDK installer does it (astral.sh versioned install
# script). Fails the build on a version mismatch.
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
      | env UV_INSTALL_DIR=/opt/musegadget/bin UV_NO_MODIFY_PATH=1 sh \
 && /opt/musegadget/bin/uv --version | grep -Fq "uv ${UV_VERSION}"

ENV PATH=/opt/musegadget/bin:$PATH \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

# venv built the installer's way: distro python + system site packages, SDK
# at the pinned commit with --no-deps, then the hash-locked requirements
# shipped inside the installed package.
RUN uv venv --python /usr/bin/python3 --system-site-packages /opt/musegadget/venv \
 && uv pip install --python /opt/musegadget/venv/bin/python --no-deps \
      "git+${MUSEGADGET_SDK_REPO}@${MUSEGADGET_SDK_REF}#subdirectory=linux" \
 && lock="$(find /opt/musegadget/venv -type f -name requirements.lock -path '*musegadget*')" \
 && [ -n "$lock" ] && [ "$(printf '%s\n' "$lock" | wc -l)" -eq 1 ] \
 && uv pip install --python /opt/musegadget/venv/bin/python --require-hashes -r "$lock" \
 && ln -s /opt/musegadget/venv/bin/musegadget /usr/local/bin/musegadget \
 && musegadget --help >/dev/null

# Accounts:
#   muse (1000)  Muse's commands run as this account (the service runs as
#                root and drops to it per command). It owns nothing secret.
#   shim (1001)  runs the HTTP shim; in group muse only to reach the socket.
# The pairing state is root-only, so neither account can read it.
RUN useradd --create-home --home-dir /home/muse --uid 1000 --user-group \
      --shell /bin/bash muse \
 && useradd --system --uid 1001 --user-group --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin shim \
 && usermod -aG muse shim \
 && install -d -m 0700 /state \
 && install -d -m 0755 /run-shared \
 && install -d -o shim -g shim -m 0700 /var/lib/muse-shim

COPY shim.py pairing_bundle.py /opt/muse-openclaw-shim/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0644 /opt/muse-openclaw-shim/shim.py /opt/muse-openclaw-shim/pairing_bundle.py \
 && chmod 0755 /usr/local/bin/entrypoint.sh \
 && python3 -I -m py_compile /opt/muse-openclaw-shim/pairing_bundle.py \
 && bash -n /usr/local/bin/entrypoint.sh

# No VOLUME on purpose: an anonymous volume would quietly keep credentials.
ENV MUSEGADGET_STATE_DIR=/state \
    MUSEGADGET_SOCKET=/run-shared/musegadget.sock \
    MUSEGADGET_RUN_AS=muse \
    SHIM_DATA_DIR=/var/lib/muse-shim \
    HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=8s --start-period=40s \
  CMD ["/usr/local/bin/entrypoint.sh", "healthcheck"]

# -g: signals go to the whole process group (service and shim alike).
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["run"]
