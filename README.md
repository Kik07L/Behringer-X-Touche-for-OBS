# X-Touch ↔ OBS Bridge

**MIDI bridge between Behringer X-Touch and OBS with Tkinter GUI.**

> ⚠️ **Warning: Experimental!**  
> Some features do not work correctly. Some faders may be mapped to unexpected controls, while others may not be mapped at all. The logic of this project needs a full review before reliable use.

---

## Requirements

- Python 3.8+
- OBS Studio ≥ 28 with WebSocket enabled (default port: 4455)
- Python packages:
  pip install mido python-rtmidi obsws-python

Behringer X-Touch MIDI controller connected to your computer.

Configuration

In the main file xtouch_obs_bridge.py, update the following settings according to your setup:
MIDI_NAME_SUBSTR = "X-TOUCH"          # Name of your X-Touch MIDI port
OBS_HOST = "192.168.1.10"            # IP address of the OBS machine
OBS_PORT = 4455                       # OBS WebSocket port
OBS_PASSWORD = "your_password"        # WebSocket password

You can also adjust the mapped faders via the FADER_CCS list.

Running the Bridge

python xtouch_obs_bridge.py

A Tkinter window will open.

Click Start to launch the bridge.

X-Touch faders and buttons will be tentatively mapped to OBS audio inputs.

Current Features

Sync X-Touch faders ↔ OBS input volumes.

Mute buttons on X-Touch to toggle audio.

VU meter updates (approximate).

Display of input names and colors on the X-Touch scribble strips.

Known Limitations

Not all faders are correctly mapped.

Some faders control incorrect parameters.

Fader and button update logic is fragile.

Labels and colors may not always sync correctly.

This is experimental code and requires a full review for stable use.

Contributing

If you want to contribute:

Focus on OBSFaderStrip and XTouchOBSControl.

MIDI ↔ OBS mapping logic is the most critical part.

Test your changes carefully, ideally on a test OBS scene.


Notes

This project is a prototype to experiment with OBS control using X-Touch. Do not use it in production without significant modifications.