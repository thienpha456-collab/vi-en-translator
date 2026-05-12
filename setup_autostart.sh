#!/bin/bash
#
# setup_autostart.sh — one-time setup for the EN↔VI translator to launch on boot.
#
# What this does:
#   1. Creates ~/translator/ as the install directory
#   2. Creates a wrapper script that loads env vars, logs output, and auto-restarts
#   3. Creates an XDG autostart entry so it launches with the desktop session
#   4. Creates ~/.translator_env for your ANTHROPIC_API_KEY (optional)
#   5. Disables screen blanking so the display stays on
#
# Prerequisite: copy translator_round.py to ~/translator/ before running this.
#
# Usage:
#   chmod +x setup_autostart.sh
#   ./setup_autostart.sh

set -e

TRANSLATOR_DIR="$HOME/translator"
AUTOSTART_DIR="$HOME/.config/autostart"
ENV_FILE="$HOME/.translator_env"
SCRIPT_NAME="translator_round.py"

echo "── Translator autostart setup ──────────────────────────"

# ─── 1. Ensure directories exist ──────────────────────────────────────────────

mkdir -p "$TRANSLATOR_DIR"
mkdir -p "$AUTOSTART_DIR"

# ─── 2. Check the translator script is in place ───────────────────────────────

if [ ! -f "$TRANSLATOR_DIR/$SCRIPT_NAME" ]; then
    echo ""
    echo "ERROR: $TRANSLATOR_DIR/$SCRIPT_NAME not found."
    echo "Copy it there first, e.g.:"
    echo "    cp $SCRIPT_NAME $TRANSLATOR_DIR/"
    exit 1
fi
echo "✓ Found $TRANSLATOR_DIR/$SCRIPT_NAME"

# ─── 3. Create the wrapper script ─────────────────────────────────────────────

cat > "$TRANSLATOR_DIR/run_translator.sh" <<'WRAPPER'
#!/bin/bash
# Wrapper: load env, log output, auto-restart on crash.

# Load API keys / config from ~/.translator_env if present
if [ -f "$HOME/.translator_env" ]; then
    set -a
    source "$HOME/.translator_env"
    set +a
fi

LOG_DIR="$HOME/translator/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/translator-$(date +%Y%m%d-%H%M%S).log"

# Keep only the 10 most recent log files
ls -1t "$LOG_DIR"/*.log 2>/dev/null | tail -n +11 | xargs -r rm 2>/dev/null || true

# Wait for the desktop / audio / display to settle after boot
sleep 5

cd "$HOME/translator"
while true; do
    echo "=== Started $(date) ===" >> "$LOG_FILE"
    python3 translator_round.py >> "$LOG_FILE" 2>&1
    CODE=$?
    echo "=== Exited (code $CODE) at $(date); restarting in 5s ===" >> "$LOG_FILE"
    sleep 5
done
WRAPPER
chmod +x "$TRANSLATOR_DIR/run_translator.sh"
echo "✓ Created $TRANSLATOR_DIR/run_translator.sh"

# ─── 4. Create the autostart .desktop entry ───────────────────────────────────

cat > "$AUTOSTART_DIR/translator.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=EN-VI Translator
Comment=Vietnamese-English voice translator with English learning mode
Exec=$TRANSLATOR_DIR/run_translator.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
echo "✓ Created $AUTOSTART_DIR/translator.desktop"

# ─── 5. Create env file template ──────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'EOF'
# Environment variables for the translator app.
# Uncomment and add your key to enable the "Simple English" panel:
# ANTHROPIC_API_KEY=sk-ant-...
EOF
    chmod 600 "$ENV_FILE"
    echo "✓ Created $ENV_FILE  (edit to add your API key)"
else
    echo "✓ $ENV_FILE already exists; leaving it alone"
fi

# ─── 6. Disable screen blanking so the display stays on ───────────────────────

if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_blanking 1 \
        && echo "✓ Screen blanking disabled" \
        || echo "  (couldn't disable blanking automatically — do it via raspi-config → Display)"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────

cat <<'DONE'

────────────────────────────────────────────────────────────
Setup complete. Two things left to do manually:

  1. Make sure the Pi auto-logs into the desktop:
       sudo raspi-config
       → System Options → Boot / Auto Login → Desktop Autologin

  2. (Optional) Add your Anthropic API key for Simple English:
       nano ~/.translator_env

Then reboot:
       sudo reboot

After reboot the translator launches automatically.

────────────────────────────────────────────────────────────
USEFUL COMMANDS

  Test the wrapper now (without rebooting):
       ~/translator/run_translator.sh

  Watch the live log:
       tail -f ~/translator/logs/translator-*.log

  Temporarily disable autostart:
       mv ~/.config/autostart/translator.desktop ~/

  Re-enable:
       mv ~/translator.desktop ~/.config/autostart/

  Stop a running instance:
       pkill -f translator_round.py
       pkill -f run_translator.sh
────────────────────────────────────────────────────────────
DONE
