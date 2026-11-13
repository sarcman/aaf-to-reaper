AAF to Reaper - Mac
===================
Turns an AAF from DaVinci Resolve (also Avid, Premiere, Pro Tools)
into a REAPER project. Works on Apple Silicon and Intel Macs.

WHAT YOU NEED
- Python 3. Most Macs already have it.
  If the app says it is missing, open Terminal and run:
      xcode-select --install

FIRST TIME ONLY
If macOS blocks the app: right-click it > Open > Open.

HOW TO USE
1. Keep the AAF and its audio files (MXF/WAV) in the same folder
   (or where Resolve exported them).
2. Double-click "AAF to Reaper".
3. Choose the AAF file(s).
4. Click "Open in REAPER".

A new folder "<AAF name> - Reaper" appears next to the AAF, holding the
REAPER project and a SoundFiles folder with the audio.
Your original files are not changed.

WHAT COMES ACROSS
- Tracks and track names
- Clips: position, length, trims
- Fade-ins, fade-outs, crossfades
- Clip volume (gain)
- Stereo clips (Resolve's separate L/R files become one stereo file)
- Start timecode, frame rate, sample rate

WHAT DOES NOT
- Track faders and pan (Resolve does not export them)
- Plug-ins and effects

If something goes wrong, see the log:
~/Library/Logs/AAF to Reaper.log
