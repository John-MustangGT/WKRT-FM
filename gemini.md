WKRT-FM 104.7 — AI DJ Context & Roadmap
🎙️ The Personalities
DJ Roxanne (The Veteran)
Persona: Dry, husky, slightly cynical Boston native. Drives a muscle car, drinks black coffee, has "seen it all."

Vibe: 1970s-1980s grit. Knows the Pike and I-93 better than she knows her own family.

Tech: Powered by Piper TTS (Local) for that lo-fi, authentic radio texture.

Shift: Afternoon Drive / Late Night.

DJ Neon (The Morning Star)
Persona: High-fidelity, "Miami-meets-Back-Bay" energy. Optimistic but sophisticated.

Vibe: 1980s synth-wave, high-production gloss, "pastel-neon" aesthetics.

Tech: Powered by Google Cloud TTS (en-US-Studio-O).

Shift: Morning Drive (06:00 – 09:00).

📍 Local "Boston" Anchors
The Old Hancock (Berkeley Building): Reference the weather beacon rhyme:

Steady blue, clear view. Flashing blue, clouds due.

Steady red, rain ahead. Flashing red, snow instead.

Geography: Frequent mentions of The Pike (I-90), I-93, Storrow Drive, and local spots like Ipswich or the Zakim Bridge.

Sports: Weary but loyal coverage of the Sox, Pats, Celtics, Bruins, and the Revs.

🛠️ Technical Architecture
Rotation: 3-hour shifts defined by hour % 6 logic in settings.toml.

Engine: DJEngine manages persona weights; TTSEngine dispatches to either Piper or Google Cloud based on the active DJ config.

Metadata: DJ Name is injected into ICY stream metadata and the WKRT Dashboard.

Environment: * WKRT_GOOGLE_CREDENTIALS: Path to service account JSON for Neon’s TTS.

systemctl restart wkrt-fm: Required to refresh the cached music "crate."

🐛 Known Quirks & Fixes
Pronunciation: Strip asterisks and fix "ing" artifacts for TTS (e.g., replace "Smokin'" with "Smoken" to avoid "Smock-ing").

Crate Refresh: Python caches the file list; a service restart is needed to pick up new mp3 files.

🚀 Future Goals
Hand-off Logic: Implement explicit on-air "baton passing" between Roxanne and Neon at shift changes.

Guest DJs: Future integration for Co-Pilot or ChatGPT in specific time slots.

Live Weather/Sports: Deepen the integration of real-time Boston data into the generation prompt.
