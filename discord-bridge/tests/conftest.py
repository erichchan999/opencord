"""bridge.py reads its settings from the environment when it is imported."""

import os
import tempfile

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("DISCORD_CHANNEL_ID", "500")
os.environ.setdefault("OPENCODE_SERVER_PASSWORD", "test-password")
os.environ.setdefault("STATE_FILE", os.path.join(tempfile.mkdtemp(), "threads.json"))
