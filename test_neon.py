
import os
import sys
import logging
from pathlib import Path
from wkrt.config import load, resolve_paths
from wkrt.tts import TTSEngine

logging.basicConfig(level=logging.INFO)

def test_neon():
    # Load config
    base = Path(__file__).parent
    cfg = load()
    cfg = resolve_paths(cfg, base)
    
    # Initialize TTS
    tts = TTSEngine(cfg)
    
    # Neon's config from settings.toml (first DJ)
    neon_cfg = cfg["djs"][0]
    
    # Debug credentials
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    print(f"DEBUG: GOOGLE_APPLICATION_CREDENTIALS = {creds_path}")
    
    test_text = "Hello Boston, this is Neon. We're smokin' through the morning drive on WKRT 104.7. The Old Hancock beacon is steady blue, so it's a clear view ahead."
    
    print(f"Testing TTS for {neon_cfg['name']} using {neon_cfg['tts_backend']}...")
    print(f"Original text: {test_text}")
    
    try:
        out_path = tts.synthesize(test_text, neon_cfg)
        print(f"Success! Saved to: {out_path}")
        
        # Move to a predictable location for the user to check
        test_out = Path("test_neon_voice.mp3")
        import shutil
        shutil.copy(out_path, test_out)
        print(f"Copy saved to: {test_out.absolute()}")
        
    except Exception as e:
        print(f"Error during synthesis: {e}")
        if "Default Credentials" in str(e) or "GOOGLE_APPLICATION_CREDENTIALS" in str(e):
            print("\nTIP: It looks like Google Cloud credentials are missing.")
            print("Please set WKRT_GOOGLE_CREDENTIALS in your .env file or environment.")

if __name__ == "__main__":
    test_neon()
