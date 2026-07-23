import os
import tempfile


# app.main exposes a production ASGI app at import time. Keep test collection from
# touching the real data directory or inheriting a developer's MiMo credential.
os.environ["NVS_DATA_DIR"] = tempfile.mkdtemp(prefix="novel-voice-studio-tests-")
os.environ["NVS_ENGINE"] = "mock"
os.environ.pop("MIMO_API_KEY", None)
