import json
import threading
import time
import os
import gi
from gi.repository import GObject, GLib
import nostr_utils

try:
    import websocket
except ImportError:
    websocket = None

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

class NostrRelay(GObject.Object):
    def __init__(self, url, on_event, on_status):
        super().__init__()
        self.url = url
        self.on_event = on_event
        self.on_status = on_status
        self.ws = None
        self.is_connected = False
        self.sub_id = None

    def start(self):
        def on_msg(ws, m):
            try:
                d = json.loads(m)
                if d[0] == "EVENT":
                    self.on_event(d[2])
                elif d[0] == "EOSE":
                    print(f"DEBUG [{self.url}]: EOSE (End of Stored Events) for {d[1]}")
                elif d[0] == "NOTICE":
                    print(f"DEBUG [{self.url}]: NOTICE: {d[1]}")
            except Exception as e:
                print(f"DEBUG [{self.url}]: Parse Error: {e}")

        def on_open(ws):
            print(f"✅ [{self.url}] Connected.")
            self.is_connected=True
            GLib.idle_add(self.on_status, self.url, "Connected")

        def on_err(ws, e):
            print(f"❌ [{self.url}] Error: {e}")
            self.is_connected=False
            GLib.idle_add(self.on_status, self.url, "Error")

        def on_close(ws, c, m):
            # print(f"⚠️ [{self.url}] Closed.")
            self.is_connected=False
            GLib.idle_add(self.on_status, self.url, "Disconnected")

        self.ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_msg, on_error=on_err, on_close=on_close)
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def subscribe(self, sub_id, filters):
        if not self.is_connected: return
        if self.sub_id and self.sub_id != sub_id:
            try: self.ws.send(json.dumps(["CLOSE", self.sub_id]))
            except: pass
        self.sub_id = sub_id
        try: self.ws.send(json.dumps(["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])))
        except: pass

    def request_once(self, sub_id, filters):
        if not self.is_connected:
            # print(f"DEBUG [{self.url}]: Cannot request '{sub_id}', not connected.")
            return
        try:
            print(f"DEBUG [{self.url}]: Sending REQ {sub_id}")
            self.ws.send(json.dumps(["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])))
        except Exception as e:
            print(f"DEBUG [{self.url}]: Send failed: {e}")

    def publish(self, event_json):
        if not self.is_connected: return
        try: self.ws.send(json.dumps(["EVENT", event_json]))
        except: pass

    def close(self):
        if self.ws: self.ws.close()

class NostrClient(GObject.Object):
    __gsignals__ = {
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
        'profile-updated': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'contacts-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'relay-list-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'metrics-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, int, int, int)),
    }

    def __init__(self, db):
        super().__init__()
        self.active_relays = {}
        self.relay_urls = set(DEFAULT_RELAYS)
        self.seen_events = set()
        self.db = db
        self.my_pubkey = None
        self.my_privkey = None
        self.requested_profiles = set()
        self.metrics = {} 
        self.config_file = os.path.join(GLib.get_user_config_dir(), "gnostr", "config.json")
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    if data.get("relays"): self.relay_urls = set(data["relays"])
            except: pass

    def save_config(self):
        d = os.path.dirname(self.config_file)
        if not os.path.exists(d): os.makedirs(d, exist_ok=True)
        try: 
            with open(self.config_file, 'w') as f: 
                json.dump({"relays": list(self.relay_urls)}, f)
        except: pass

    def set_keys(self, pub, priv): self.my_pubkey = pub; self.my_privkey = priv

    def connect_all(self):
        for url in list(self.relay_urls): self.add_relay_connection(url)

    def add_relay_connection(self, url):
        if url in self.active_relays: return
        r = NostrRelay(url, self._handle_event, self._handle_status)
        r.start()
        self.active_relays[url] = r

    def add_relay(self, url):
        if url not in self.relay_urls:
            self.relay_urls.add(url); self.save_config()
            self.add_relay_connection(url); self.emit('relay-list-updated')
            self.publish_relay_list()

    def remove_relay(self, url):
        if url in self.relay_urls:
            self.relay_urls.remove(url); self.save_config()
            if url in self.active_relays: self.active_relays[url].close(); del self.active_relays[url]
            self.emit('relay-list-updated'); self.publish_relay_list()

    def fetch_user_relays(self):
        if not self.my_pubkey: return
        print(f"DEBUG: Requesting relay list (Kind 10002) for {self.my_pubkey[:8]}...")
        filter = {"kinds": [10002], "authors": [self.my_pubkey], "limit": 1}

        # Use unique ID to force fresh response
        sub_id = f"relays_{self.my_pubkey[:8]}_{int(time.time())}"

        count = 0
        for r in self.active_relays.values(): 
            if r.is_connected:
                r.request_once(sub_id, filter)
                count += 1

        print(f"DEBUG: Sent relay request to {count} connected relays.")

    def publish_relay_list(self):
        if not self.my_privkey: return
        event = {"pubkey": self.my_pubkey, "created_at": int(time.time()), "kind": 10002, 
                 "tags": [['r', u] for u in self.relay_urls], "content": ""}
        signed = nostr_utils.sign_event(event, self.my_privkey)
        if signed: 
            for r in self.active_relays.values(): r.publish(signed)

    def get_ref_id(self, tags):
        for t in tags: 
            if t[0] == 'e': return t[1]
        return None

    def _handle_event(self, ev):
        eid = ev.get('id'); kind = ev['kind']; pubkey = ev['pubkey']
        tags = ev.get('tags', [])

        # DEBUG: Catch-all to confirm if we receive anything
        if kind == 10002 or kind == 3:
            print(f"DEBUG: Received Metadata Event Kind {kind} from {pubkey[:8]}")

        # Metrics handling
        target = self.get_ref_id(tags)
        if target:
            if target not in self.metrics: self.metrics[target] = {'likes':0,'reposts':0,'replies':0}
            updated = False
            if kind == 7: self.metrics[target]['likes']+=1; updated=True
            elif kind == 6: self.metrics[target]['reposts']+=1; updated=True
            elif kind == 1: self.metrics[target]['replies']+=1; updated=True
            if updated: 
                m = self.metrics[target]
                GLib.idle_add(self.emit, 'metrics-updated', target, m['likes'], m['reposts'], m['replies'])

        if eid in self.seen_events: return
        self.seen_events.add(eid)
        self.db.save_event(ev)

        if kind == 0:
            self.db.save_profile(pubkey, ev['content'], ev['created_at'])
            GLib.idle_add(self.emit, 'profile-updated', pubkey)

        elif kind == 3:
            if pubkey == self.my_pubkey:
                # 1. Contacts
                c = nostr_utils.extract_followed_pubkeys(ev)
                self.db.save_contacts(self.my_pubkey, c)
                GLib.idle_add(self.emit, 'contacts-updated')

                # 2. Relay Fallback (Old Standard)
                # Kind 3 content is often: {"wss://...": {"read": true, "write": true}}
                try:
                    if ev['content']:
                        relays_json = json.loads(ev['content'])
                        if isinstance(relays_json, dict):
                            print(f"DEBUG: Found relays in Kind 3 content: {list(relays_json.keys())}")
                            self._merge_relays(relays_json.keys())
                except: pass

        elif kind == 10002:
            if pubkey == self.my_pubkey:
                print("DEBUG: Processing Kind 10002 (Relay List)")
                nr = [t[1] for t in tags if t[0]=='r' and len(t) > 1]
                if nr:
                    self._merge_relays(nr)
                else:
                    print("DEBUG: Kind 10002 received but contained no 'r' tags.")

        elif kind == 1:
            GLib.idle_add(self.emit, 'event-received', eid, pubkey, ev['content'], json.dumps(tags))

    def _merge_relays(self, new_list):
        changed = False
        for r in new_list:
            r = r.rstrip("/") # Normalize
            if r.startswith("ws") and r not in self.relay_urls:
                print(f"DEBUG: Adding new relay found in sync: {r}")
                self.relay_urls.add(r)
                self.add_relay_connection(r)
                changed = True
        if changed:
            self.save_config()
            GLib.idle_add(self.emit, 'relay-list-updated')

    def _handle_status(self, url, status): self.emit('status-changed', status)
    def subscribe(self, sub_id, filters):
        for r in self.active_relays.values(): r.subscribe(sub_id, filters)
    def fetch_contacts(self):
        if self.my_pubkey: self.subscribe("sub_contacts", {"kinds": [3], "authors": [self.my_pubkey], "limit": 1})
    def fetch_profile(self, pubkey):
        if pubkey in self.requested_profiles: return
        self.requested_profiles.add(pubkey)
        for r in self.active_relays.values(): r.request_once(f"meta_{pubkey[:8]}", {"kinds": [0], "authors": [pubkey], "limit": 1})
    def fetch_thread(self, root_id):
        f1 = {"ids": [root_id]}
        f2 = {"kinds": [1], "#e": [root_id], "limit": 50}
        f3 = {"kinds": [6, 7], "#e": [root_id], "limit": 100}
        self.subscribe(f"thread_{root_id}", [f1, f2, f3])
    def close(self): 
        for r in self.active_relays.values(): r.close()
