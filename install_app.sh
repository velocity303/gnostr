#!/bin/bash
set -e
echo "==> [Gnostr] Installing..."

# 1. Install Binaries
install -D -m 755 src/main.py /app/bin/gnostr
cp -v src/*.py /app/bin/ 2>/dev/null || true
rm -f /app/bin/main.py

# 2. Install Desktop File
# Ensure the target filename matches the App ID exactly: me.velocitynet.Gnostr.desktop
install -D -m 644 data/me.velocitynet.Gnostr.desktop /app/share/applications/me.velocitynet.Gnostr.desktop

# 3. Install AppData (Metainfo)
install -D -m 644 data/me.velocitynet.Gnostr.metainfo.xml /app/share/metainfo/me.velocitynet.Gnostr.metainfo.xml

# 4. Install Icons
# A. Scalable App Icon
install -D -m 644 data/icons/hicolor/scalable/apps/me.velocitynet.Gnostr.svg /app/share/icons/hicolor/scalable/apps/me.velocitynet.Gnostr.svg

# B. Symbolic Icon (MISSING IN YOUR ORIGINAL SCRIPT)
install -D -m 644 data/icons/hicolor/symbolic/apps/me.velocitynet.Gnostr-symbolic.svg /app/share/icons/hicolor/symbolic/apps/me.velocitynet.Gnostr-symbolic.svg

# 5. Set PYTHONPATH
export PYTHONPATH=$PYTHONPATH:/app/lib/python3.12/site-packages
