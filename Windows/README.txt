AAF to Reaper - Windows
=======================
Turns an AAF from DaVinci Resolve (also Avid, Premiere, Pro Tools)
into a REAPER project.

WHAT YOU NEED
- Python 3 (free), installed once:
  https://www.python.org/downloads/windows/
  In the installer, tick "Add python.exe to PATH".
- Keep all files of this folder together.

HOW TO USE
1. Keep the AAF and its audio files (MXF/WAV) in the same folder
   (or where Resolve exported them).
2. Double-click "AAF to Reaper.bat"
   (or drag AAF files onto it).
3. Choose the AAF file(s).
4. Click "Open in REAPER".

A new folder "<AAF name> - Reaper" appears next to the AAF, holding the
REAPER project and a SoundFiles folder with the audio.
Your original files are not changed.

Tip: for a desktop icon, right-click the .bat > Create shortcut, then
shortcut Properties > Change Icon > choose "AAF to Reaper.ico".

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
%LOCALAPPDATA%\AAF to Reaper\AAF to Reaper.log
