# Pulley, with git and the Docker command-line tools it drives.
# It talks to the Docker that runs on the machine, through the socket mounted into it.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && printf 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
        "$(. /etc/os-release && echo "$VERSION_CODENAME")" "$(dpkg --print-architecture)" \
        > /etc/apt/sources.list.d/docker.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY pulley ./pulley
RUN pip install --no-cache-dir .

ENV PULLEY_HOME=/data
VOLUME /data
EXPOSE 8790
CMD ["pulley", "serve", "--host", "0.0.0.0"]
