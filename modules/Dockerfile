# Dockerfile for cross-compiling all Porter modules for Raspberry Pi
# Target: aarch64 (ARM64) Debian 12 (Bookworm)

FROM debian:bookworm

# Avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Add ARM64 architecture for cross-compilation dependencies
RUN dpkg --add-architecture arm64

# Install C/C++ cross-compilation toolchain and build essentials
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    curl \
    pkg-config \
    gcc-aarch64-linux-gnu \
    g++-aarch64-linux-gnu \
    binutils-aarch64-linux-gnu \
    libusb-1.0-0-dev \
    libusb-1.0-0-dev:arm64 \
    libudev-dev \
    libudev-dev:arm64 \
    libopencv-dev:arm64 \
    && rm -rf /var/lib/apt/lists/*

# Install Rust via rustup
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Add the aarch64 cross-compilation target for Rust
RUN rustup target add aarch64-unknown-linux-gnu

# Configure Cargo to use the aarch64 linker
RUN mkdir -p /root/.cargo && \
    echo '[target.aarch64-unknown-linux-gnu]\nlinker = "aarch64-linux-gnu-gcc"' > /root/.cargo/config.toml

# Set environment variables for C/C++ cross-compilation
ENV CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc
ENV CXX_aarch64_unknown_linux_gnu=aarch64-linux-gnu-g++
ENV CC=aarch64-linux-gnu-gcc
ENV CXX=aarch64-linux-gnu-g++
ENV PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig
ENV PKG_CONFIG_SYSROOT_DIR=/

# Copy the CMake toolchain file for C++ cross-compilation
COPY toolchain-rpi.cmake /opt/toolchain-rpi.cmake
ENV CMAKE_TOOLCHAIN_FILE=/opt/toolchain-rpi.cmake

# Set Vimba X SDK path for Alvium Camera Module
# The SDK is mounted via volume at /workspace/Alvium-Camera-Module/alvium-sdk
ENV VIMBA_X_HOME=/workspace/Alvium-Camera-Module/alvium-sdk

WORKDIR /workspace

CMD ["/bin/bash"]
