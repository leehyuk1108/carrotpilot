#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

export QCOM_PRIORITY=12

OPENPILOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPAT_LIB_DIR="$OPENPILOT_ROOT/third_party/runtime_libs"
LOCAL_LIBYUV_DIR="$OPENPILOT_ROOT/third_party/libyuv/larch64/lib"
if [ -d "$COMPAT_LIB_DIR" ]; then
  export LD_LIBRARY_PATH="${COMPAT_LIB_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
if [ -d "$LOCAL_LIBYUV_DIR" ]; then
  export LD_LIBRARY_PATH="${LOCAL_LIBYUV_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="12.4"
fi

export STAGING_ROOT="/data/safe_staging"

if [ -S /var/tmp/weston/wayland-0 ]; then
  export XDG_RUNTIME_DIR="/var/tmp/weston"
  export WAYLAND_DISPLAY="wayland-0"
  export QT_QPA_PLATFORM="wayland-egl"
fi
