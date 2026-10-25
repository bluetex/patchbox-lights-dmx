# Melody Capture

A browser prototype for turning a hummed or played monophonic melody into MIDI and MusicXML.

## Run on macOS

Microphone access requires a secure browser context. From this directory, start a local server:

```sh
python3 -m http.server 8000
```

Then open <http://localhost:8000> in Safari or Chrome and allow microphone access. Choose a time signature, tempo, tonic, and major/minor mode. You can pick the tonic or choose **Set key by humming**; it gives a visible four-count, then plays and listens for `do–mi–sol–mi–do–sol–do`, one note per click. After key selection, **Start listening** plays the key’s tonic chord, then counts in before recording. The audio click uses pitches from the selected key. Choose **Finish take** to enable MIDI and MusicXML downloads. Both exports include the selected tempo, meter, and key. Open MusicXML in MuseScore, Dorico, or another notation editor to inspect and correct it.

## Current scope

- Live pitch estimation from the selected system microphone
- Record one melodic line at a time
- A staff preview that chooses treble, bass, alto, or tenor clef from the recorded pitch range
- Key selection by picker or by singing a major triad pattern along with a visible/audio count-in
- User-selected meter and tempo, with an audible click and visual count-in pulse
- Standard MIDI file and MusicXML exports
- Audio analysis stays in the browser

This is an early prototype. It estimates the strongest fundamental pitch and works best with one clear sound at a time. Chords, overlapping instruments, note spelling, rests, and rhythm are not yet reliably inferred. The selected meter and tempo are written into exports, but note durations are still approximate and need review in notation software.

## Possible next steps

1. Add audio file import and waveform/pitch review.
2. Improve onset and rhythm detection, then add editable notes and rests.
3. Add a polyphonic transcription mode using an audio-to-MIDI model.
4. Compare the live single-line workflow with open projects such as [Warf](https://github.com/monco/Warf), and offline transcription with [NeuralNote](https://github.com/DamRsn/NeuralNote) or [MuScriptor](https://github.com/muscriptor/muscriptor).
