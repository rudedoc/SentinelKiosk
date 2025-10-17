# G13/G13Validator.py
import time, serial
from typing import Callable, Optional, Dict, List, Tuple, Iterable


class G13Validator:
    # ---- ccTalk headers ----
    H_ADDRESS_POLL             = 253
    H_REQ_MANUFACTURER_ID      = 246
    H_REQ_PRODUCT_CODE         = 244
    H_REQ_SOFTWARE_REV         = 241
    H_MODIFY_INHIBIT_STATUS    = 231
    H_REQUEST_INHIBIT_STATUS   = 230
    H_MODIFY_MASTER_INHIBIT    = 228
    H_REQUEST_MASTER_INHIBIT   = 227
    H_READ_BUFFERED_CREDIT     = 229   # returns 11 bytes: counter + 5*(type,path)
    H_REQUEST_COIN_ID          = 184   # [coin_type] -> 6 ASCII chars
    H_MODIFY_SORTER_PATHS      = 210   # 5-byte payload: paths for types 1..5

    ERROR_CODES: Dict[int, str] = {
        1: "Reject coin",
        2: "Inhibited coin",
        8: "2nd close coin error",
        10: "Credit sensor not ready",
        14: "Credit sensor blocked",
        16: "Credit sequence error",
        17: "Coin going backwards",
        20: "Coin-on-string mechanism",
        254: "Coin return mechanism",
        255: "Unspecified alarm",
    }

    # ---------- lifecycle ----------
    def __init__(self, port: str, addr: Optional[int] = None, *, host: int = 1,
                 baud: int = 9600, timeout: float = 1.0, gap: float = 0.03):
        self.port = port
        self.addr = addr
        self.host = host
        self.baud = baud
        self.timeout = timeout
        self.gap = gap
        self.ser: Optional[serial.Serial] = None
        self._coin_types: Dict[int, str] = {}      # type -> coin_id (e.g., EU050A)
        self._last_counter: Optional[int] = None   # rolling 8-bit credit counter

    def open(self):
        self.ser = serial.Serial(
            self.port, self.baud,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout, write_timeout=self.timeout,
            rtscts=False, dsrdtr=False, xonxoff=False
        )
        time.sleep(0.15)
        if self.addr is None:
            self.addr = self._probe_address()
        return self

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    # ---------- high-level one-liners ----------
    def enable_all(self) -> None:
        """Allow acceptance: set all coin inhibits ON and master inhibit ON."""
        self._set_inhibits_all(True)
        self._set_master_inhibit(True)

    def status(self) -> Dict[str, str]:
        """Small status dict: IDs + inhibits."""
        ids = self.get_ids()
        inh = self._get_inhibits()
        mi  = self._get_master_inhibit()
        return {
            "manufacturer": ids["manufacturer"],
            "product": ids["product"],
            "software": ids["software"],
            "inhibits": inh.hex(" ").upper() if inh else "N/A",
            "master_inhibit": f"{mi:02X}" if mi is not None else "N/A",
        }

    def enable_and_poll(self, on_event: Optional[Callable[[dict], None]] = None,
                        interval: float = 0.2) -> None:
        """
        Convenience: enable acceptance, build coin map, then poll forever.
        Calls `on_event(ev)` for each credit/error. If `on_event` is None, prints.
        """
        ids = self.get_ids()
        self.enable_all()
        self._coin_types = self.build_coin_type_map()
        self._sync_counter()
        while True:
            for ev in self.poll_once():
                if on_event:
                    on_event(ev)
                else:
                    self._print_event(ev)
            time.sleep(interval)

    # ---------- public utilities ----------
    def get_ids(self) -> Dict[str, str]:
        man = self._xfer(self.addr, self.H_REQ_MANUFACTURER_ID)
        prod = self._xfer(self.addr, self.H_REQ_PRODUCT_CODE)
        soft = self._xfer(self.addr, self.H_REQ_SOFTWARE_REV)
        man_s  = (man[4:-1].decode("ascii","ignore") if man else "")
        prod_s = (prod[4:-1].decode("ascii","ignore") if prod else "")
        soft_s = (soft[4:-1].decode("ascii","ignore") if soft else "")
        return {"manufacturer": man_s, "product": prod_s, "software": soft_s}

    def build_coin_type_map(self) -> Dict[int, str]:
        """Query types 1..16 -> coin IDs (skips empty/blank)."""
        m: Dict[int, str] = {}
        for t in range(1, 17):
            cid = self.request_coin_id(t)
            if cid and not cid.isspace():
                m[t] = cid
        return m

    def request_coin_id(self, coin_type: int) -> Optional[str]:
        r = self._xfer(self.addr, self.H_REQUEST_COIN_ID, bytes([coin_type]))
        if not r:
            return None
        s = r[4:-1].decode("ascii", "ignore")
        return s if len(s) == 6 else None

    def coin_id_to_label(self, coin_id: str) -> str:
        """'EU200A' -> '€2.00 (EU200A)' (simple EUR pretty-print)."""
        if len(coin_id) != 6:
            return coin_id
        cc = coin_id[:2]
        num = coin_id[2:5]
        try:
            val = int(num) / 100.0
            symbol = "€" if cc == "EU" else cc
            return f"{symbol}{val:.2f} ({coin_id})"
        except ValueError:
            return coin_id

    def value_from_coin_id(self, coin_id: str) -> Optional[int]:
        """Return euro-cents for IDs like EU050A -> 50; else None."""
        if not coin_id or len(coin_id) < 5:
            return None
        try:
            return int(coin_id[2:5])
        except ValueError:
            return None

    def set_sorter_paths(self, paths_for_types_1_to_5: Iterable[int]) -> bool:
        """
        Map ccTalk coin types 1..5 to sorter paths 1..5.
        Pass exactly 5 integers (1..5). Returns True if ACKed.
        """
        p = list(paths_for_types_1_to_5)
        if len(p) != 5 or any(not (1 <= x <= 5) for x in p):
            raise ValueError("Provide exactly five paths in 1..5")
        return self._xfer(self.addr, self.H_MODIFY_SORTER_PATHS, bytes(p)) is not None

    # ---------- polling ----------
    def poll_once(self) -> List[dict]:
        """
        Read the buffered credits once and return only the *new* events since last call.
        Each event dict for credit has: {
            'type': 'credit', 'coin_type': int, 'coin_id': str|None,
            'label': str, 'value_cents': int|None, 'path': int, 'counter': int
        }
        Errors: {'type':'error','code':int,'desc':str,'counter':int}
        """
        res = self._read_buffered_credit_pairs()
        if not res:
            return []
        counter, pairs = res   # pairs: list of 5 tuples (coin_type, path)

        # compute delta via rolling 8-bit counter
        if self._last_counter is None:
            self._last_counter = counter
            return []
        diff = (counter - self._last_counter) & 0xFF
        self._last_counter = counter
        if diff == 0:
            return []

        # Newest events are at the head on many G13 builds; if yours are tail-newest, swap this slice/reverse.
        n_new = min(diff, 5)
        newest = pairs[:n_new]

        out: List[dict] = []
        for coin_type, path in newest:
            if coin_type == 0:
                continue
            if 1 <= coin_type <= 32:
                cid = self._coin_types.get(coin_type) or self.request_coin_id(coin_type)
                if cid:
                    self._coin_types[coin_type] = cid
                label = self.coin_id_to_label(cid) if cid else f"type {coin_type}"
                val = self.value_from_coin_id(cid) if cid else None
                out.append({
                    "type": "credit",
                    "coin_type": coin_type,
                    "coin_id": cid,
                    "label": label,
                    "value_cents": val,
                    "path": path,
                    "counter": counter,
                })
            else:
                out.append({
                    "type": "error",
                    "code": coin_type,
                    "desc": self.ERROR_CODES.get(coin_type, "Unknown"),
                    "counter": counter,
                })
        return out

    # ---------- internals ----------
    @staticmethod
    def _csum(b: bytes) -> int:
        return (-sum(b)) & 0xFF

    @staticmethod
    def _frame(dest: int, src: int, header: int, data: bytes = b"") -> bytes:
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        core = bytearray([dest, len(data), src, header]) + data
        core.append(G13Validator._csum(core))
        return core

    def _xfer(self, dest: int, header: int, data: bytes = b"", timeout: Optional[float] = None,
              gap: Optional[float] = None) -> Optional[bytes]:
        """Write frame (drain echo), then read reply frame with checksum+address checks."""
        assert self.ser, "Port not open"
        tmo = self.timeout if timeout is None else timeout
        gp  = self.gap if gap is None else gap
        pkt = self._frame(dest, self.host, header, data)

        # clear stale bytes, write, small turnaround, drain echo
        self.ser.reset_input_buffer()
        self.ser.write(pkt)
        self.ser.flush()
        time.sleep(gp)
        self._drain_echo(len(pkt), timeout=0.12)

        hdr = self._read_exact(4, tmo)
        if not hdr: return None
        dest_r, length, src_r, header_r = hdr
        payload = self._read_exact(length, tmo) if length else b""
        chk = self._read_exact(1, tmo)
        if chk is None: return None
        reply = bytes(hdr) + payload + chk

        # checksum + addressing sanity
        if (sum(reply) & 0xFF) != 0:
            return None
        # ensure it's to us (host) and from device (unless broadcast dest=0)
        if dest_r != self.host or (dest != 0 and src_r != self.addr):
            return None
        return reply

    def _probe_address(self) -> int:
        # 1) broadcast address poll (dest 0) -> device returns its address (1 byte)
        for _ in range(3):
            r = self._xfer(0, self.H_ADDRESS_POLL, b"", timeout=1.0)
            if r and r[1] == 1 and len(r) >= 6:
                return r[4]
        # 2) fallback: try a few common addresses
        for a in (2, 1, 3, 4, 5):
            r = self._xfer(a, self.H_REQ_MANUFACTURER_ID, b"", timeout=1.0)
            if r:
                return a
        raise RuntimeError("G13 address not found")

    def _set_inhibits_all(self, enable: bool) -> bool:
        mask = bytes([0xFF, 0xFF]) if enable else bytes([0x00, 0x00])
        return self._xfer(self.addr, self.H_MODIFY_INHIBIT_STATUS, mask) is not None

    def _get_inhibits(self) -> Optional[bytes]:
        r = self._xfer(self.addr, self.H_REQUEST_INHIBIT_STATUS)
        return r[4:-1] if r else None

    def _set_master_inhibit(self, allow: bool) -> bool:
        val = bytes([0x01 if allow else 0x00])
        return self._xfer(self.addr, self.H_MODIFY_MASTER_INHIBIT, val) is not None

    def _get_master_inhibit(self) -> Optional[int]:
        r = self._xfer(self.addr, self.H_REQUEST_MASTER_INHIBIT)
        return r[4] if r and len(r) >= 6 else None

    def _read_buffered_credit_pairs(self) -> Optional[Tuple[int, List[Tuple[int, int]]]]:
        """Return (counter, [(type,path)*5]) from the 11-byte payload."""
        r = self._xfer(self.addr, self.H_READ_BUFFERED_CREDIT)
        if not r:
            return None
        data = r[4:-1]
        if len(data) != 11:
            return None
        counter = data[0]
        events_raw = data[1:]
        pairs = [(events_raw[i], events_raw[i+1]) for i in range(0, 10, 2)]
        return counter, pairs

    def _sync_counter(self) -> None:
        """Initialize the rolling counter without emitting historical events."""
        res = self._read_buffered_credit_pairs()
        if res:
            self._last_counter = res[0]

    # --- low-level serial helpers (echo & reads) ---
    def _read_exact(self, n: int, timeout: float) -> Optional[bytes]:
        assert self.ser, "Port not open"
        self.ser.timeout = timeout
        buf = self.ser.read(n)
        return buf if len(buf) == n else None

    def _drain_echo(self, n: int, timeout: float = 0.15) -> None:
        assert self.ser, "Port not open"
        end = time.time() + timeout
        got = 0
        while got < n and time.time() < end:
            chunk = self.ser.read(n - got)
            if not chunk:
                break
            got += len(chunk)

    # --- default print formatting for events ---
    def _print_event(self, ev: dict) -> None:
        if ev["type"] == "credit":
            label = ev.get("label") or f"type {ev['coin_type']}"
            path = ev.get("path", 0)
            print(f"[CREDIT] {label} (type {ev['coin_type']}, path {path})")
        else:
            print(f"[ERROR ] {ev['code']} - {ev['desc']}")