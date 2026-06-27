# /opt/kokoro/server.py
import io
import numpy as np
import soundfile as sf
from fastapi import FastAPI, Response
from kokoro import KPipeline

app = FastAPI()
# Initializes pipeline for American English ('a')
pipeline = KPipeline(lang_code='a')

@app.get("/tts")
def text_to_speech(text: str, voice: str = "af_heart"):
    audio_chunks = []
    for _, _, audio in pipeline(text, voice=voice, speed=1, split_pattern=r"\n+"):
        audio_chunks.append(audio)
    
    combined = np.concatenate(audio_chunks) if len(audio_chunks) > 1 else audio_chunks[0]
    wav_buffer = io.BytesIO()
    sf.write(wav_buffer, combined, 24000, format="WAV")
    return Response(content=wav_buffer.getvalue(), media_type="audio/wav")