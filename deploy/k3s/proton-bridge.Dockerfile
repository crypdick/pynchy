FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ARG PROTON_BRIDGE_VERSION=3.24.2-1
ARG PROTON_BRIDGE_SHA256=8cbcad0010b1769bf5b1fb0f1523c0dd8347189d5ed7883417f0ecca77bfdf91

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl gnupg pass \
    && curl --fail --location --show-error \
        --output /tmp/protonmail-bridge.deb \
        "https://github.com/ProtonMail/proton-bridge/releases/download/v${PROTON_BRIDGE_VERSION%-*}/protonmail-bridge_${PROTON_BRIDGE_VERSION}_amd64.deb" \
    && echo "${PROTON_BRIDGE_SHA256}  /tmp/protonmail-bridge.deb" | sha256sum --check --strict \
    && apt-get install --yes --no-install-recommends /tmp/protonmail-bridge.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/protonmail-bridge.deb \
    && useradd --create-home --home-dir /home/bridge --uid 3000 bridge

COPY proton-bridge-entrypoint.sh /usr/local/bin/pynchy-proton-bridge

USER 3000:3000
ENV HOME=/home/bridge
WORKDIR /home/bridge

ENTRYPOINT ["/usr/local/bin/pynchy-proton-bridge"]
CMD ["--noninteractive"]
