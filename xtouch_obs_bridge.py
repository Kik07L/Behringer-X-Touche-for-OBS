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

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
MIDI_NAME_SUBSTR = "X-TOUCH"
OBS_HOST = "192.168.1.10"
OBS_PORT = 4455
OBS_PASSWORD = "5lTGhDUiwGeKYcVx"
POLL_INTERVAL = 0.3
MIDI_CHANNEL = 0
# faders CC (à adapter selon ce que ta X-Touch émet)
FADER_CCS = [0, 1, 2, 3, 4, 5, 6, 7]

# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

def midi_to_linear(v): return max(0.0, min(1.0, v/127.0))
def linear_to_midi(v): return int(round(max(0.0, min(1.0, v))*127))

def find_midi_ports(substr):
    ins, outs = mido.get_input_names(), mido.get_output_names()
    return (next((n for n in ins if substr.upper() in n.upper()), None),
            next((n for n in outs if substr.upper() in n.upper()), None))

# ------------------------------------------------------------
class ObsBridge:
    def __init__(self):
        self.req = None
    def connect(self):
        self.req = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        self.req.get_version()  # test
        logging.info("Connecté OBS WebSocket")
    def get_audio_inputs_for_current_scene(self):
        # Fetch all inputs and filter for audio sources
        all_inputs = self.req.get_input_list().inputs
        audio_kinds = [
            "wasapi_input_capture", "wasapi_output_capture", "wasapi_process_output_capture",
            "dshow_input", "ffmpeg_source", "pulse_input_capture", "pulse_output_capture"
        ]
        audio_inputs = [inp["inputName"] for inp in all_inputs if inp["inputKind"] in audio_kinds]
        logging.info(f"Audio inputs found (global): {audio_inputs}")
        return audio_inputs
    def get_input_volume(self, name):
        try:
            resp = self.req.get_input_volume(name)
            return float(resp.input_volume)
        except: return None
    def set_input_volume(self, name, val):
        try: self.req.set_input_volume(name, val)
        except Exception as e: logging.warning("set volume %s: %s", name, e)

# ------------------------------------------------------------
class MidiObsThread(threading.Thread):
    def __init__(self, midi_in, midi_out, obs_bridge, log_callback):
        super().__init__(daemon=True)
        self.midi_in_name, self.midi_out_name = midi_in, midi_out
        self.obs = obs_bridge
        self.running = False
        self.log = log_callback
        self.last_vals = {}

    def run(self):
        self.obs.connect()
        # auto-map selon scène
        inputs = self.obs.get_audio_inputs_for_current_scene()
        self.fader_map = {i: inputs[i] for i in range(min(len(inputs), len(FADER_CCS)))}
        self.log(f"Faders mappés : {self.fader_map}")
        self.inport = mido.open_input(self.midi_in_name)
        self.outport = mido.open_output(self.midi_out_name)
        self.running = True
        # Init: synchronize X-Touch faders to OBS values
        for cc, name in self.fader_map.items():
            val = self.obs.get_input_volume(name)
            if val is not None:
                pitch_val = int(round(-8192 + val * 16383))
                logging.info(f"[INIT] Send to X-Touch: pitchwheel {cc} value {pitch_val} for {name}")
                self.outport.send(mido.Message("pitchwheel", channel=cc, pitch=pitch_val))
                self.last_vals[cc] = val
        # Extra: after a short delay, refresh all fader positions to ensure sync
        time.sleep(0.5)
        for cc, name in self.fader_map.items():
            val = self.obs.get_input_volume(name)
            if val is not None:
                pitch_val = int(round(-8192 + val * 16383))
                logging.info(f"[REFRESH] Send to X-Touch: pitchwheel {cc} value {pitch_val} for {name}")
                self.outport.send(mido.Message("pitchwheel", channel=cc, pitch=pitch_val))
                self.last_vals[cc] = val
        while self.running:
            # traiter messages MIDI entrants
            for msg in self.inport.iter_pending():
                logging.info(f"[MIDI IN] {msg}")
                # Handle pitchwheel for faders
                if msg.type == "pitchwheel" and msg.channel in self.fader_map:
                    lin = (msg.pitch + 8192) / 16383.0
                    logging.info(f"[MIDI->OBS] Set {self.fader_map[msg.channel]} to {lin:.3f}")
                    if abs(self.last_vals.get(msg.channel, -1) - lin) > 0.005:
                        self.obs.set_input_volume(self.fader_map[msg.channel], lin)
                        self.last_vals[msg.channel] = lin
                # Optionally, still handle control_change for other controls
                if msg.type == "control_change" and msg.control in self.fader_map and msg.channel == MIDI_CHANNEL:
                    lin = midi_to_linear(msg.value)
                    logging.info(f"[MIDI->OBS] Set {self.fader_map[msg.control]} to {lin:.3f}")
                    if abs(self.last_vals.get(msg.control, -1) - lin) > 0.005:
                        self.obs.set_input_volume(self.fader_map[msg.control], lin)
                        self.last_vals[msg.control] = lin
            # polling OBS -> envoyer aux moteurs
            for cc, name in self.fader_map.items():
                val = self.obs.get_input_volume(name)
                if val is not None:
                    cur = self.last_vals.get(cc, -1)
                    if abs(cur - val) > 0.005:
                        pitch_val = int(round(-8192 + val * 16383))
                        logging.info(f"[OBS->MIDI] Send to X-Touch: pitchwheel {cc} value {pitch_val} for {name}")
                        self.outport.send(mido.Message("pitchwheel", channel=cc, pitch=pitch_val))
                        self.last_vals[cc] = val
            time.sleep(POLL_INTERVAL)

    def stop(self):
        self.running = False
        try: self.inport.close(); self.outport.close()
        except: pass

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

    def log(self, txt):
        self.log_box.insert(tk.END, txt + "\n")
        self.log_box.see(tk.END)

    def toggle(self):
        if self.thread and self.thread.running:
            self.thread.stop()
            self.thread = None
            self.btn.config(text="Démarrer")
            self.log("Arrêt du bridge.")
        else:
            inport, outport = find_midi_ports(MIDI_NAME_SUBSTR)
            if not inport or not outport:
                self.log(f"Port MIDI '{MIDI_NAME_SUBSTR}' introuvable")
                return
            bridge = ObsBridge()
            self.thread = MidiObsThread(inport, outport, bridge, self.log)
            self.thread.start()
            self.btn.config(text="Arrêter")
            self.log(f"Bridge lancé. MIDI IN: {inport} / OUT: {outport}")

# ------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()