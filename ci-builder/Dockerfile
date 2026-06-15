FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    libgtk-3-dev libwebkit2gtk-4.1-dev libappindicator3-dev libfuse2 fakeroot dpkg \
    python3 python3-pip python3-dev build-essential libgl1 libglib2.0-0 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install ImpactAcquire SDK once — baked into the image so CI pipelines skip this step
COPY ImpactAcquire-x86_64-linux-3.7.0.sh /tmp/
RUN bash /tmp/ImpactAcquire-x86_64-linux-3.7.0.sh --mode unattended --prefix /opt/ImpactAcquire || true \
    && test -f /opt/ImpactAcquire/lib/x86_64/libmvDeviceManager.so.3 \
    && rm /tmp/ImpactAcquire-x86_64-linux-3.7.0.sh

ENV LD_LIBRARY_PATH=/opt/ImpactAcquire/lib/x86_64
ENV GENICAM_GENTL64_PATH=/opt/ImpactAcquire/lib/x86_64

RUN pip3 install uv --break-system-packages
