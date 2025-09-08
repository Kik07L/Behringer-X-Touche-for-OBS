#!/usr/bin/env python3
"""
Bridge X-Touch ↔ OBS avec GUI Tkinter.
Nécessite : pip install mido python-rtmidi obsws-python
OBS >= 28 avec WebSocket activé (port 4455)
"""

import tkinter as tk
from tkinter import ttk
import threading, time, logging, sys
import mido
import obsws_python as obs
from collections import OrderedDict

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
MIDI_NAME_SUBSTR = "X-TOUCH"
OBS_HOST = "192.168.1.10"
OBS_PORT = 4455
OBS_PASSWORD = "5lTGhDUiwGeKYcVx"
POLL_INTERVAL = 0.01  # Fast polling for OBS->MIDI sync, MIDI->OBS is instant
MIDI_CHANNEL = 1
FADER_CCS = [0, 1, 2, 3, 4, 5, 6, 7]

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

def midi_to_linear(v): return max(0.0, min(1.0, v/127.0))
def linear_to_midi(v): return int(round(max(0.0, min(1.0, v))*127))

MIN_DB = -95.0
MAX_DB = 0.0

def fader_to_db(fader_val):
    return MIN_DB + (MAX_DB - MIN_DB) * fader_val

def db_to_fader(db_val):
    return (db_val - MIN_DB) / (MAX_DB - MIN_DB)

def db_to_multiplier(db_val):
    return 10 ** (db_val / 20.0)

def multiplier_to_db(mult):
    import math
    if mult <= 0:
        return MIN_DB
    return 20.0 * math.log10(mult)

def find_midi_ports(substr):
    ins, outs = mido.get_input_names(), mido.get_output_names()
    x_touch_ports = [n for n in outs if substr.upper() in n.upper()]
    filtered_ports = [n for n in x_touch_ports if 'MIDIOUT2' not in n.upper()]
    outport = filtered_ports[0] if filtered_ports else (x_touch_ports[0] if x_touch_ports else None)
    inport = next((n for n in ins if substr.upper() in n.upper()), None)
    return (inport, outport)

# Color constants and color matching (from MainDisplay.py/consts.py)
scribble_colors = [
    (255, 0, 0),    # red
    (0, 255, 0),    # green
    (255, 255, 0),  # yellow
    (0, 0, 255),    # blue
    (255, 0, 255),  # magenta
    (0, 255, 255),  # cyan
    (255, 255, 255) # white
]
def color_distance(c1, c2):
    return sum((a-b)**2 for a, b in zip(c1, c2))
def match_color(rgb):
    # Find closest color index for X-Touch scribble strip
    dists = [color_distance(rgb, c) for c in scribble_colors]
    return dists.index(min(dists))

def obs_source_color(idx):
    # Assign a default color per channel (cycling through scribble_colors)
    return scribble_colors[idx % len(scribble_colors)]

# ------------------------------------------------------------
# Component Classes (inspired by MackieControl structure)
# ------------------------------------------------------------
class OBSFaderStrip:
    def __init__(self, index, obs_bridge, midi_out):
        self.index = index
        self.obs_bridge = obs_bridge
        self.midi_out = midi_out
        self.input_name = None
        self.last_val = 0.0
        self.touched = False
        self.mute_state = False
        self.color = obs_source_color(index)

    def assign_input(self, input_name):
        self.input_name = input_name

    def sync_from_obs(self):
        if not self.input_name:
            return
        val = self.obs_bridge.get_input_volume(self.input_name)
        if val is not None:
            db_val = multiplier_to_db(val)
            fader_val = db_to_fader(db_val)
            self.send_fader_position(fader_val)
            self.last_val = fader_val
        # Sync mute LED
        mute = self.obs_bridge.get_input_mute(self.input_name)
        self.set_mute_led(mute)
        self.mute_state = mute
        # Update LCD label and color (always 7 chars, always all 8 channels)
        self.send_lcd_label(self.input_name)
        time.sleep(0.01)
        self.send_lcd_color(self.color)
        time.sleep(0.01)

    def send_fader_position(self, value):
        pitch_val = int(round(-8192 + value * 16383))
        pitch_val = max(-8192, min(8191, pitch_val))
        msg = mido.Message("pitchwheel", channel=self.index, pitch=pitch_val)
        self.midi_out.send(msg)

    def send_lcd_label(self, text):
        # Always 7 ASCII chars, channel 0-7
        label = text[:7].ljust(7)
        safe_label = ''.join(c if 32 <= ord(c) <= 126 else ' ' for c in label)
        sysex = [0x00, 0x00, 0x66, 0x14, 0x12, self.index] + [ord(c) for c in safe_label]
        self.midi_out.send(mido.Message('sysex', data=sysex))

    def send_lcd_color(self, rgb):
        # Send color to scribble strip (SysEx, 0-6 index)
        color_idx = match_color(rgb)
        sysex = [0x00, 0x00, 0x66, 0x14, 0x13, self.index, color_idx]
        self.midi_out.send(mido.Message('sysex', data=sysex))

    def set_from_midi(self, value):
        if not self.input_name:
            return
        db_val = fader_to_db(value)
        multiplier = db_to_multiplier(db_val)
        self.obs_bridge.set_input_volume(self.input_name, multiplier)
        self.last_val = value

    def set_mute_led(self, mute):
        # MCU mute LED: NOTE_ON, note=16+index, velocity=127 (on) or 0 (off)
        note = 16 + self.index
        velocity = 127 if mute else 0
        msg = mido.Message('note_on', note=note, velocity=velocity)
        self.midi_out.send(msg)

    def toggle_mute(self):
        if self.input_name:
            new_mute = not self.mute_state
            self.obs_bridge.set_input_mute(self.input_name, new_mute)
            self.set_mute_led(new_mute)
            self.mute_state = new_mute

    def send_vu_meter(self, level):
        # Correct VU meter: Channel Pressure (Aftertouch) on channel=index, value=0-14
        meter_val = min(int(level * 15.2), 14)
        msg = mido.Message('aftertouch', channel=self.index, value=meter_val)
        self.midi_out.send(msg)

class OBSBridge:
    def __init__(self):
        self.req = None
    def connect(self):
        self.req = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        self.req.get_version()
    def get_audio_inputs(self):
        resp = self.req.get_input_list()
        if hasattr(resp, 'inputs'):
            all_inputs = resp.inputs
        elif isinstance(resp, dict) and 'inputs' in resp:
            all_inputs = resp['inputs']
        else:
            all_inputs = []
        audio_kinds = [
            "wasapi_input_capture", "wasapi_output_capture", "wasapi_process_output_capture",
            "pulse_input_capture", "pulse_output_capture"
        ]
        audio_inputs = []
        for inp in all_inputs:
            if inp.get("inputKind") in audio_kinds:
                try:
                    vol = self.get_input_volume(inp["inputName"])
                    if vol is not None:
                        audio_inputs.append(inp["inputName"])
                except Exception:
                    pass
        return audio_inputs
    def get_input_volume(self, name):
        try:
            resp = self.req.get_input_volume(name)
            if hasattr(resp, 'input_volume_mul'):
                return float(resp.input_volume_mul)
            elif hasattr(resp, 'input_volume'):
                return float(resp.input_volume)
            elif hasattr(resp, 'inputVolume'):
                return float(resp.inputVolume)
            else:
                return None
        except Exception as e:
            logging.warning(f"[DEBUG] get_input_volume failed for '{name}': {e}")
            return None
    def set_input_volume(self, name, val):
        try: self.req.set_input_volume(name, val)
        except Exception as e: logging.warning("set volume %s: %s", name, e)
    def get_input_mute(self, name):
        try:
            resp = self.req.get_input_mute(name)
            if hasattr(resp, 'input_muted'):
                return bool(resp.input_muted)
            elif hasattr(resp, 'inputMuted'):
                return bool(resp.inputMuted)
            else:
                return False
        except Exception as e:
            logging.warning(f"[DEBUG] get_input_mute failed for '{name}': {e}")
            return False
    def set_input_mute(self, name, mute):
        try:
            self.req.set_input_mute(name, mute)
        except Exception as e:
            logging.warning(f"set mute {name}: {e}")

class XTouchOBSControl:
    def __init__(self, midi_in_name, midi_out_name, obs_bridge, log_callback):
        self.midi_in_name = midi_in_name
        self.midi_out_name = midi_out_name
        self.obs_bridge = obs_bridge
        self.log = log_callback
        self.fader_strips = []
        self.running = False
        self.inport = None
        self.outport = None
        self.fader_map = OrderedDict()
        self.fader_touched = {}
        self.button_map = self._default_button_map()

    def _default_button_map(self):
        # Map X-Touch mute buttons (notes 16-23) to fader strips
        # You can expand this dict to map more buttons to OBS actions
        return {16+i: (i, 'mute') for i in range(len(FADER_CCS))}

    def setup(self):
        self.obs_bridge.connect()
        audio_inputs = self.obs_bridge.get_audio_inputs()
        self.outport = mido.open_output(self.midi_out_name)
        self.inport = mido.open_input(self.midi_in_name)
        for idx, input_name in enumerate(audio_inputs[:len(FADER_CCS)]):
            strip = OBSFaderStrip(idx, self.obs_bridge, self.outport)
            strip.assign_input(input_name)
            self.fader_strips.append(strip)
            self.fader_map[idx] = input_name
            self.fader_touched[idx] = False
        self.log(f"Faders mappés : {self.fader_map}")
        # MCU handshake
        self.send_mcu_handshake()
        time.sleep(0.2)
        # Sync faders and labels
        for strip in self.fader_strips:
            strip.sync_from_obs()
            strip.send_lcd_label(strip.input_name)
            time.sleep(0.05)
        # After setup, start VU meter polling
        threading.Thread(target=self.vu_meter_poll, daemon=True).start()

    def send_mcu_handshake(self):
        sysex = [0x00, 0x00, 0x66, 0x14, 0x10, 0x01]
        self.outport.send(mido.Message('sysex', data=sysex))

    def run(self):
        self.running = True
        def lcd_label_refresh():
            while self.running:
                audio_inputs = self.obs_bridge.get_audio_inputs()
                for strip in self.fader_strips:
                    label = strip.input_name if strip.input_name in audio_inputs else strip.input_name
                    strip.send_lcd_label(label)
                    time.sleep(0.01)
                    strip.send_lcd_color(strip.color)
                    time.sleep(0.01)
                time.sleep(5)
        threading.Thread(target=lcd_label_refresh, daemon=True).start()
        while self.running:
            for msg in self.inport.iter_pending():
                # Handle fader touch
                if msg.type == "note_on" and msg.note == 104:
                    self.fader_touched[0] = msg.velocity > 0
                # Handle fader movement
                if msg.type == "pitchwheel" and msg.channel in self.fader_map:
                    fader_val = (msg.pitch + 8192) / 16383.0
                    self.fader_strips[msg.channel].set_from_midi(fader_val)
                if msg.type == "control_change" and msg.control in self.fader_map and msg.channel == msg.control:
                    fader_val = midi_to_linear(msg.value)
                    self.fader_strips[msg.control].set_from_midi(fader_val)
                # Handle button presses (mute, etc.)
                if msg.type == "note_on" and msg.note in self.button_map and msg.velocity > 0:
                    idx, action = self.button_map[msg.note]
                    if action == 'mute':
                        self.fader_strips[idx].toggle_mute()
            for strip in self.fader_strips:
                if not self.fader_touched.get(strip.index, False):
                    strip.sync_from_obs()
                    time.sleep(0.01)
            time.sleep(POLL_INTERVAL)

    def stop(self):
        self.running = False
        try:
            self.inport.close()
            self.outport.close()
        except:
            pass

    def vu_meter_poll(self):
        # Poll OBS for audio levels and update VU meters
        while self.running:
            for strip in self.fader_strips:
                if strip.input_name:
                    # Get OBS volume multiplier (0.0-1.0+)
                    val = self.obs_bridge.get_input_volume(strip.input_name)
                    if val is not None:
                        # For VU, use dB to linear, then clamp 0-1
                        import math
                        db = multiplier_to_db(val)
                        # Map dB range (-95 to 0) to 0-1
                        vu = max(0.0, min(1.0, (db - MIN_DB) / (MAX_DB - MIN_DB)))
                        strip.send_vu_meter(vu)
                        time.sleep(0.01)
            time.sleep(0.05)
# ------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("X-Touch ↔ OBS Bridge")
        ttk.Label(root, text="Bridge MIDI → OBS").pack()
        self.log_box = tk.Text(root, width=70, height=15)
        self.log_box.pack()
        self.btn = ttk.Button(root, text="Démarrer", command=self.toggle)
        self.btn.pack(pady=5)
        self.thread = None
        self.xtouch = None

    def log(self, txt):
        self.log_box.insert(tk.END, txt + "\n")
        self.log_box.see(tk.END)

    def toggle(self):
        if self.xtouch and self.xtouch.running:
            self.xtouch.stop()
            self.xtouch = None
            self.btn.config(text="Démarrer")
            self.log("Arrêt du bridge.")
        else:
            inport, outport = find_midi_ports(MIDI_NAME_SUBSTR)
            if not inport or not outport:
                self.log(f"Port MIDI '{MIDI_NAME_SUBSTR}' introuvable")
                return
            bridge = OBSBridge()
            self.xtouch = XTouchOBSControl(inport, outport, bridge, self.log)
            self.xtouch.setup()
            self.thread = threading.Thread(target=self.xtouch.run, daemon=True)
            self.thread.start()
            self.btn.config(text="Arrêter")
            self.log(f"Bridge lancé. MIDI IN: {inport} / OUT: {outport}")

# ------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()