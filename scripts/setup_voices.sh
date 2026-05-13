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
echo "==> Done. Test with:"
echo "    python main.py --test-tts \"You're listening to WKRT, 104.7 FM.\""
