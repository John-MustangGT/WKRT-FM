#!/usr/bin/env bash
# Download Piper TTS binary and voice model
# Run once: bash setup_voices.sh

set -e

VOICES_DIR="./voices"
PIPER_VERSION="2023.11.14-2"
VOICE="en_US-lessac-high"

mkdir -p "$VOICES_DIR"

echo "==> Detecting platform..."
ARCH=$(uname -m)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')

if [[ "$OS" == "linux" && "$ARCH" == "aarch64" ]]; then
    PIPER_PLATFORM="linux_aarch64"  # Pi Zero 2W / Pi 4
elif [[ "$OS" == "linux" && "$ARCH" == "x86_64" ]]; then
    PIPER_PLATFORM="linux_x86_64"   # Ubuntu laptop
elif [[ "$OS" == "darwin" ]]; then
    PIPER_PLATFORM="macos_x64"      # Mac
else
    echo "Unknown platform: $OS $ARCH"
    exit 1
fi

echo "==> Platform: $PIPER_PLATFORM"

# ── Download Piper binary ────────────────────────────────────────────────────
PIPER_URL="https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_${PIPER_PLATFORM}.tar.gz"
PIPER_ARCHIVE="/tmp/piper.tar.gz"

if ! command -v piper &>/dev/null; then
    echo "==> Downloading Piper binary..."
    curl -L "$PIPER_URL" -o "$PIPER_ARCHIVE"
    tar -xzf "$PIPER_ARCHIVE" -C /tmp/
    sudo cp /tmp/piper/piper /usr/local/bin/piper
    sudo chmod +x /usr/local/bin/piper
    echo "==> Piper installed at /usr/local/bin/piper"
else
    echo "==> Piper already installed: $(which piper)"
fi

# ── Download voice model ─────────────────────────────────────────────────────
VOICE_BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high"
ONNX_FILE="${VOICES_DIR}/${VOICE}.onnx"
JSON_FILE="${VOICES_DIR}/${VOICE}.onnx.json"

if [[ ! -f "$ONNX_FILE" ]]; then
    echo "==> Downloading voice model: $VOICE"
    curl -L "${VOICE_BASE_URL}/en_US-lessac-high.onnx" -o "$ONNX_FILE"
else
    echo "==> Voice model already present: $ONNX_FILE"
fi

if [[ ! -f "$JSON_FILE" ]]; then
    echo "==> Downloading voice config..."
    curl -L "${VOICE_BASE_URL}/en_US-lessac-high.onnx.json" -o "$JSON_FILE"
else
    echo "==> Voice config already present: $JSON_FILE"
fi

echo ""

# ── Kokoro ONNX model files (optional) ──────────────────────────────────────
# Only needed if any DJ uses tts_backend = "kokoro" in settings.toml.
# Install the Python packages first:  pip install kokoro-onnx soundfile
KOKORO_MODEL="${VOICES_DIR}/kokoro-v1.0.onnx"
KOKORO_VOICES="${VOICES_DIR}/kokoro-voices-v1.0.bin"
# Model files are hosted on the kokoro-onnx GitHub releases (not HuggingFace,
# which serves git-lfs pointer files instead of the real binaries via curl).
KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
# For Raspberry Pi, use the int8 quantized model (88 MB vs 310 MB, much faster):
#   KOKORO_MODEL_FILE="kokoro-v1.0.int8.onnx"
KOKORO_MODEL_FILE="${KOKORO_MODEL_FILE:-kokoro-v1.0.onnx}"

_kokoro_download() {
    local url="$1" dest="$2" min_bytes="$3" label="$4"
    curl -L --fail --show-error "$url" -o "$dest"
    local size
    size=$(wc -c < "$dest")
    if [[ "$size" -lt "$min_bytes" ]]; then
        echo "ERROR: $label download looks wrong (got ${size} bytes, expected ≥${min_bytes})."
        echo "       Check the URL or download manually: $url"
        rm -f "$dest"
        exit 1
    fi
    echo "==> Downloaded: $dest (${size} bytes)"
}

if [[ "${INSTALL_KOKORO:-}" == "1" ]]; then
    echo "==> Downloading Kokoro ONNX model files (v1.0)..."
    if [[ ! -f "$KOKORO_MODEL" ]]; then
        _kokoro_download "${KOKORO_BASE}/${KOKORO_MODEL_FILE}" "$KOKORO_MODEL" 50000000 "kokoro model"
    else
        echo "==> Kokoro model already present: $KOKORO_MODEL"
    fi
    if [[ ! -f "$KOKORO_VOICES" ]]; then
        _kokoro_download "${KOKORO_BASE}/voices-v1.0.bin" "$KOKORO_VOICES" 1000000 "kokoro voices"
    else
        echo "==> Kokoro voices already present: $KOKORO_VOICES"
    fi
else
    echo "==> Skipping Kokoro model download (set INSTALL_KOKORO=1 to download)."
    echo "    e.g.: INSTALL_KOKORO=1 bash scripts/setup_voices.sh"
fi

echo ""
echo "==> Done. Test with:"
echo "    python main.py --test-tts \"You're listening to WKRT, 104.7 FM.\""
