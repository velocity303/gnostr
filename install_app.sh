#!/bin/bash
set -e
echo "==> [Gnostr] Installing..."

# 1. Install Binaries
# -D creates parent directories. -m 755 sets executable permission.
install -D -m 755 src/main.py /app/bin/gnostr
cp -v src/*.py /app/bin/ 2>/dev/null || true
# Cleanup potential duplicate
rm -f /app/bin/main.py

# 2. Install Desktop File (Must go to /app/share/applications)
install -D -m 644 data/me.velocitynet.Gnostr.desktop /app/share/applications/me.velocitynet.Gnostr.desktop

# 3. Install AppData
install -D -m 644 data/me.velocitynet.Gnostr.metainfo.xml /app/share/metainfo/me.velocitynet.Gnostr.metainfo.xml

# 4. Install Icons (Must go to /app/share/icons/...)
install -D -m 644 data/icons/hicolor/scalable/apps/me.velocitynet.Gnostr.svg /app/share/icons/hicolor/scalable/apps/me.velocitynet.Gnostr.svg

# 5. Set PYTHONPATH for runtime
export PYTHONPATH=$PYTHONPATH:/app/lib/python3.12/site-packages
