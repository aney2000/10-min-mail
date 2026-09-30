# syntax=docker/dockerfile:1
#
# Two-stage build for 10 Minute Mail.
#
# Stage 1 builds a wheel. Stage 2 installs that wheel into a clean
# image, so the thing that ships contains no compilers, no build
# caches, no test suite and no source tree -- a smaller image and a
# smaller attack surface. For a pure-Python project the size saving is
# modest; the reduction in what an attacker finds if they get a shell
# is not.

# --------------------------------------------------------------------------- #
# Stage 1: build
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

WORKDIR /build

# Copy only what the build needs, and in dependency order. pyproject
# and the source are all setuptools requires; leaving the rest out
# keeps this layer cacheable.
COPY pyproject.toml README.md ./
COPY src ./src

# --no-cache-dir: the wheel cache is dead weight in a layer we discard.
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist

# --------------------------------------------------------------------------- #
# Stage 2: runtime
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED: without it Python buffers stdout when it is a pipe,
# so `docker logs` shows nothing until the buffer fills -- which looks
# exactly like a hung container.
# PYTHONDONTWRITEBYTECODE: .pyc files in a container are write traffic
# for no benefit; the process starts once.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Run as an unprivileged user.
#
# This is a mail server: it accepts data from anyone who can reach the
# port. As root, any bug in it is a container compromise. Creating a
# user costs one line and removes that entire class of escalation.
#
# It is also why the SMTP port defaults to 1025 rather than 25 -- ports
# below 1024 need root to bind. Docker publishes 25:1025 if the
# standard port is wanted, so the privileged bind happens in Docker's
# networking layer instead of inside this process.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser

# The database lives on a volume so mailboxes survive a container
# restart. Owned by appuser, because a root-owned directory would make
# the unprivileged process fail on first write.
RUN mkdir -p /data && chown appuser:appuser /data

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

USER appuser
WORKDIR /home/appuser

# Bind all interfaces: inside a container 127.0.0.1 is unreachable from
# the host, so the service would appear dead. This is safe *here* in a
# way it is not as a library default -- the container's network
# exposure is the operator's explicit choice, made in compose or on the
# docker run command line.
ENV TMM_HTTP_HOST=0.0.0.0 \
    TMM_HTTP_PORT=8000 \
    TMM_SMTP_HOST=0.0.0.0 \
    TMM_SMTP_PORT=1025 \
    TMM_DATABASE_PATH=/data/ten_min_mail.db \
    TMM_MAIL_DOMAIN=localhost.test \
    TMM_LOG_LEVEL=INFO

EXPOSE 8000 1025
VOLUME ["/data"]

# Uses the /health endpoint, which performs a real database read -- so
# a container whose database has gone away is reported unhealthy rather
# than merely "running".
#
# start-period gives the app time to boot before failures count, so a
# slow start is not mistaken for a crash loop.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

# Exec form (not shell form): the process becomes PID 1 directly and
# receives SIGTERM from `docker stop`, so the lifespan shutdown -- which
# cancels the sweeper and closes the database -- actually runs. In shell
# form a /bin/sh wrapper would take the signal and the app would be
# killed uncleanly after the timeout.
CMD ["python", "-m", "ten_min_mail"]
