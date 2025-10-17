# NV9/nv9_core.py
import sys, struct, time, serial, threading
from dataclasses import dataclass
from typing import Callable, Optional, Dict, List

@dataclass(frozen=True)
class NV9Event:
    code: int
    name: str
    channel: Optional[int] = None
    value: Optional[int] = None
    reason: Optional[str] = None

class NV9Validator:
    """
    Synchronous SSP client for ITL NV9 validators with simple callbacks.

    Design notes:
    - The class does *not* own its own thread. Poll it from your loop (GUI worker or CLI).
    - All I/O is performed over a `serial.Serial` configured for SSP framing and CRC16.
    - Public callbacks (`on_status`, `on_error`, `on_event`) allow UI/CLI hooks without Qt deps.
    """

    # === SSP transport ===
    STX = 0x7F  # Start-of-frame marker used by SSP; frames begin with one 0x7F and use 0x7F byte-stuffing.  # GA138, Transport Layer

    # === Commands (host -> device) ===
    # Generic / session control
    CMD_SSP_SYNC                 = 0x11  # Must be sent first; after OK the *next* command uses seq=0.         # SYNC
    CMD_SSP_HOST_PROTOCOL_VERSION= 0x06  # Tell the device which protocol level to use for events.             # Host Protocol Version

    # Device capability / config
    CMD_SSP_SETUP_REQUEST        = 0x05  # Returns dataset info: channels, multipliers, (>=v6) ISO codes/values.  # Setup Request
    CMD_SSP_SET_INHIBITS         = 0x02  # Enable/disable channels; bitmask LSB = channel 1.                   # Set Inhibits

    # Run-state control
    CMD_SSP_ENABLE               = 0x0A  # Enable, start accepting notes. - indicated by red LED being on on bezel
    CMD_SSP_DISABLE              = 0x09  # Stop accepting notes.

    # Runtime helpers
    CMD_SSP_POLL                 = 0x07  # Fetch all events since last poll; may return multiple events.       # Poll
    CMD_SSP_LAST_REJECT_CODE     = 0x17  # One-byte reason for the most recent rejection.                      # Last Reject Code
    CMD_SSP_HOLD                 = 0x18  # Keep a note in escrow by refreshing the timeout (≤10 s window).     # Hold

    # === Generic OK/Events (device -> host) ===
    RSP_SSP_OK                   = 0xF0  # Generic success reply to a command.                                 # Generic OK

    # Event headers returned inside POLL
    RSP_SSP_SLAVE_RESET          = 0xF1  # Device has (re)started.                                             # Event: Slave Reset
    RSP_SSP_NOTE_READ            = 0xEF  # Data=0 scanning; >0 => escrowed on that channel.                    # Event: Read
    RSP_SSP_CREDIT_NOTE          = 0xEE  # Data=channel credited (credit point).                               # Event: Note Credit
    RSP_SSP_REJECTING            = 0xED  # returning note to user (pre-REJECTED)
    RSP_SSP_REJECTED             = 0xEC  # Rejected to user; no data bytes (query 0x17 for reason).            # Event: Rejected
    RSP_SSP_STACKING             = 0xCC  # Moving from escrow to stacker (no data).                            # Event: Stacking
    RSP_SSP_STACKED              = 0xEB  # Fully stacked (no data).                                            # Event: Stacked
    RSP_SSP_DISABLED             = 0xE8  # Device disabled (no data).                                          # Event: Disabled

    # === Common reject reasons (extend as needed) ===
    REJECT_REASONS = {
        0x00: "No reason / Accepted",          # 0x00 appears when no reject occurred.                          # Reject Codes
        0x01: "Note too long",
        0x02: "Note too short",
        0x03: "Invalid note",
        0x04: "Accept gate not ready",
        0x05: "Channel inhibited",
        # ... add more from the table as you encounter them in your dataset/firmware
    }

    # === IO / protocol timings (tuned to spec-friendly values) ===
    READ_TIMEOUT_S     = 0.5   # Serial read timeout; per-call read window.
    WRITE_TIMEOUT_S    = 0.5   # Serial write timeout.
    SETUP_DEADLINE_S   = 1.2   # Maximum time to wait for Setup Request reply (larger packets).
    DEFAULT_DEADLINE_S = 1.0   # General command reply timeout (per SSP guidance).


    def __init__(self, port: str, baud: int = 9600, *, slave_id: int = 0x00, host_protocol_version: int | None = None):
        """
        Bind an NV9 validator to a serial port and initialize protocol/book-keeping state.

        Parameters
        ----------
        port : str
            OS serial device name (e.g. 'COM5' on Windows, '/dev/ttyUSB0' on Linux).
        baud : int, default 9600
            SSP line speed. NV9 defaults to 9600 unless reconfigured.
        slave_id : int, default 0x00
            SSP bus address (0..0x7D). Most single-device setups use 0x00.
        host_protocol_version : int | None
            If set, we’ll request the device to use this SSP protocol version
            right after SETUP (affects event formats/features).
        """
        # --- Connection descriptor (pure configuration) ---
        self.port = port
        self.baud = baud

        # Clamp to valid 7-bit SSP address range; NV9 default is 0x00.
        self.slave_id = max(0, min(0x7D, slave_id))

        # If not None, we’ll issue HOST_PROTOCOL_VERSION after SETUP.
        self.requested_protocol_version = host_protocol_version

        # Underlying pyserial object. Created in connect(), closed in disconnect().
        self.serial_port: Optional[serial.Serial] = None

        # --- Protocol state (transport-level bookkeeping) ---
        # SSP uses a 1-bit sequence flag that MUST toggle on each successful exchange.
        # We initialize to 0x80 (unknown/“next toggle will set it to 0x00”); after a successful SYNC,
        # we explicitly force it to 0x00 so the next command complies with the spec.
        self.sequence_bit: int = 0x80

        # --- Dataset / currency info (populated by SETUP) ---
        # num_channels: how many note channels the current dataset exposes (1..n).
        self.num_channels: int = 0

        # channel_value_map maps channel -> face value (in “app units”, see value_multiplier).
        # We fill this after parsing SETUP. Until then, we fall back to a simple EUR map.
        self.channel_value_map: Dict[int, int] = {}

        # Multiplier applied to raw values from SETUP (depends on protocol/dataset).
        # For example, values might be reported in cents and you want whole currency units.
        self.value_multiplier: int = 1

        # Optional ISO currency code derived from SETUP (e.g., "EUR", "USD").
        self.currency: str = "EUR"

        # Fallback values used *only* before SETUP is parsed.
        # Note: these are whole euros (5, 10, 20, ...) for human-friendly display.
        # If your app logic requires minor units, multiply accordingly.
        self.euro_values = {1: 5, 2: 10, 3: 20, 4: 50, 5: 100, 6: 200, 7: 500}

        # --- UI / application callbacks (optional) ---
        # These are invoked from the calling thread (synchronous usage),
        # or bridged to signals in the Qt worker.
        self.on_event: Optional[Callable[[NV9Event], None]] = None  # per-event hook
        self.on_status: Optional[Callable[[str], None]] = None      # human-readable status
        self.on_error: Optional[Callable[[str], None]] = None       # human-readable errors

        # Cooperative stop flag for hosts that want to check/coordinate shutdown.
        self._stop_flag = threading.Event()

        # --- Convenience/backoff for auto re-enable behavior ---
        # When POLL reports DISABLED, we may try to re-enable. To avoid hammering the device
        # during genuine fault conditions (cashbox full, path open, etc.), apply a small backoff.
        self._last_enable_attempt = 0.0
        self._enable_backoff_s = 1.0


    # ---------- Public, sync control ----------
    def connect(self) -> bool:
        """
        Open the serial port and prepare for SSP communication.

        Returns
        -------
        bool
            True on success, False if the port could not be opened.

        Notes
        -----
        - NV9 defaults to 9600 8N1 (8 data bits, no parity, 1 stop bit).
        - We configure short read/write timeouts so higher-level methods
        can retry/abort quickly instead of blocking indefinitely.
        - After opening, we flush both RX and TX buffers to discard any
        stale data left from a previous session (important before SYNC).
        """
        try:
            # Open the port with NV9 defaults (8N1 framing).
            # pyserial raises SerialException if the port is unavailable.
            self.serial_port = serial.Serial(
                self.port,
                self.baud,
                bytesize=serial.EIGHTBITS,   # 8 data bits
                parity=serial.PARITY_NONE,   # no parity
                stopbits=serial.STOPBITS_ONE,# 1 stop bit
                timeout=self.READ_TIMEOUT_S, # per-read timeout
                write_timeout=self.WRITE_TIMEOUT_S, # per-write timeout
            )

            # Clear out any garbage bytes that may have accumulated.
            # This avoids mis-framing on the very first SYNC command.
            try:
                self.serial_port.reset_input_buffer()
                self.serial_port.reset_output_buffer()
            except Exception:
                # Some OS/drivers may not support buffer reset — safe to ignore.
                pass

            # Give the device a tiny window to settle after port open.
            time.sleep(0.02)

            # Report status back to application/UI layer.
            self._status(f"Connected to {self.port} @ {self.baud} bps")
            return True

        except (serial.SerialException, ValueError) as exc:
            # Catch both pyserial errors (e.g. port not found, permissions)
            # and invalid configuration values.
            self._error(f"Error connecting to {self.port}: {exc}")
            return False


    def initialize_device(self) -> bool:
        """
        Run the standard SSP bring-up sequence for an NV9.

        Sequence
        --------
        1) SYNC
        - Aligns framing and resets the device-side sequence logic.
        - Per spec, the *next* command after a successful SYNC must use seq=0.
        2) SETUP_REQUEST
        - Populates dataset/currency/channel info (if supported).
        - Non-fatal if it fails: we can still run with sensible defaults.
        3) (Optional) HOST_PROTOCOL_VERSION
        - Locks the device to the requested protocol level if the caller asked.
        - If not supported, we continue with the device default.
        4) SET_INHIBITS
        - Enables channels via bitmask. If we don’t know the exact count yet,
            we send a conservative 2-byte “all enabled” mask (common for NV9).
        5) ENABLE
        - Starts accepting notes.

        Returns
        -------
        bool
            True if the device is initialized and accepting; False on a hard failure.
        """
        # Sanity: require an open serial port (connect() must be called first).
        if not (self.serial_port and self.serial_port.is_open):
            self._error("initialize_device() called without an open serial port. Call connect() first.")
            return False

        # After power-up or reset, hosts must start with sequence bit = 0x00.
        # _sync() will also force this upon success, but we set it early for clarity.
        self.sequence_bit = 0x00

        # 1) SYNC — align transport and reset device-side sequencing.
        if not self._sync():
            # SYNC failing usually indicates cabling/baud/port issues or framing noise.
            return False

        # 2) SETUP_REQUEST — try to discover channels/currency/values.
        if not self._setup_request():
            # Non-fatal: we fall back to defaults (e.g., euro_values) and carry on.
            self._status("SETUP_REQUEST failed; continuing with default channel/value mapping.")

        # 3) HOST_PROTOCOL_VERSION (optional) — lock the protocol level if requested.
        if self.requested_protocol_version is not None:
            if not self.set_host_protocol_version(self.requested_protocol_version):
                # Some firmware/datasets won’t support all versions; that’s okay.
                self._status("Host Protocol Version not set; continuing with device default.")

        # 4) SET_INHIBITS — enable channels (bitmask LSB = channel 1).
        # If num_channels is unknown, we send a 2-byte 0xFFFF mask (typical for NV9).
        if not self._set_inhibits():
            # If this fails, there is little we can do: device won’t accept anything.
            return False

        # 5) ENABLE — start accepting notes.
        if not self.enable():
            # ENABLE may fail if the validator reports a hard fault (e.g., cashbox missing).
            return False

        return True


    def enable(self) -> bool:
        """Put the validator into accepting state."""
        response = self._send_command(self.CMD_SSP_ENABLE)
        return bool(response and response[2] == self.RSP_SSP_OK)

    def disable(self) -> bool:
        """Disable note acceptance (safe to call during disconnect)."""
        response = self._send_command(self.CMD_SSP_DISABLE)
        return bool(response and response[2] == self.RSP_SSP_OK)

    def hold(self, ms: int = 500) -> bool:
        """Send HOLD (escrow hold) for the specified duration (milliseconds)."""
        ms = max(0, min(10_000, ms))
        params = struct.pack("<H", ms)
        rsp = self._send_command(self.CMD_SSP_HOLD, params)
        return bool(rsp and rsp[2] == self.RSP_SSP_OK)

    def disconnect(self):
        """Disable and close the serial port. Swallows errors but reports them."""
        try:
            if self.serial_port and self.serial_port.is_open:
                try:
                    self.disable()
                finally:
                    self.serial_port.close()
                self._status("Disconnected.")
        except Exception as e:
            self._error(f"Disconnect error: {e}")

    def stop(self):
        """Set a cooperative stop flag that user code can check in its poll loop."""
        self._stop_flag.set()

    # ---------- Polling (sync) ----------
    def poll_once(self) -> List[NV9Event]:
        """
        Send a single POLL command and translate the device's reply into NV9Event objects.

        Behavior
        --------
        - Returns a list of zero or more NV9Event items gathered from the device.
        - Emits self.on_event(event) for each event if a callback is registered.
        - Special cases:
            * SLAVE_RESET: attempts an inline reinitialization sequence.
            * DISABLED:    rate-limited attempt to re-enable to avoid fighting real faults.

        Protocol notes
        --------------
        - A successful POLL reply is:
            [addr|seq, length, 0xF0 (=OK), <event stream...>]
        where "<event stream>" is a concatenation of 1-byte event codes
        and optional data bytes per event type. Some events carry no data.
        """
        # Transmit POLL and get the raw payload (already CRC-verified by _read_full_response()).
        payload = self._send_command(self.CMD_SSP_POLL)

        events: List[NV9Event] = []

        # Validate basic OK header and that at least one event byte could exist.
        # payload[2] == RSP_SSP_OK (0xF0); event bytes begin at payload[3:].
        if not (payload and len(payload) > 3 and payload[2] == self.RSP_SSP_OK):
            return events  # No events or non-OK; return empty list

        # Extract the variable-length "event stream" from the reply.
        event_stream = payload[3:]

        # Parse all events in the stream into NV9Event objects.
        for event in self._process_events(event_stream):
            events.append(event)

            # Surface each event to the application/UI if a callback is registered.
            if self.on_event:
                self.on_event(event)

            # --- Special handling: device reset ---------------------------------
            if event.code == self.RSP_SSP_SLAVE_RESET:
                # Device reports a (re)boot; we must re-run the bring-up sequence.
                self._status("Device reset; reinitializing…")

                # After reset, per spec, we start from sequence bit = 0 and SYNC again.
                self.sequence_bit = 0x00

                # Minimal re-init: SYNC -> SETUP (best-effort) -> (optional) protocol version ->
                #                  SET_INHIBITS -> ENABLE
                if self._sync() and self._setup_request():
                    # If caller requested a specific protocol version, re-apply it now.
                    if self.requested_protocol_version is not None:
                        self.set_host_protocol_version(self.requested_protocol_version)
                    if self._set_inhibits() and self.enable():
                        self._status("Reinitialized after reset.")
                    else:
                        self._error("Reinit incomplete after reset.")
                else:
                    self._error("Reinit failed after reset.")

                # Once a reset occurs, we stop processing any remaining events from this poll.
                break

            # --- Special handling: device disabled -------------------------------
            elif event.code == self.RSP_SSP_DISABLED:
                # The device is disabled (could be host-initiated or a genuine fault like
                # "cashbox missing"). To avoid hammering the device during real faults,
                # re-enable at most once per _enable_backoff_s seconds.
                now = time.monotonic()
                if now - self._last_enable_attempt >= self._enable_backoff_s:
                    self._last_enable_attempt = now
                    self.enable()

        return events


    # ---------- Internal protocol ----------
    def _sync(self) -> bool:
        """Perform SSP SYNC to align sequence/framing on both sides."""
        response = self._send_command(self.CMD_SSP_SYNC)
        ok = bool(response and response[2] == self.RSP_SSP_OK)
        if ok:
            # After SYNC, both sides expect seq=0 on *next* command
            self.sequence_bit = 0x00
        else:
            self._error("SYNC failed")
        return ok

    def _setup_request(self) -> bool:
        """Ask the validator for its dataset/capabilities.

        NOTE: This implementation accepts the response but does not parse fields yet.
        Hook here to parse and fill: num_channels, value_multiplier, currency, channel_value_map.
        """
        response = self._send_command(self.CMD_SSP_SETUP_REQUEST)
        if response and response[2] == self.RSP_SSP_OK:
            # TODO: parse dataset to populate channel_value_map/num_channels
            return True
        return False

    def set_host_protocol_version(self, version: int) -> bool:
        """Set Host Protocol Version (affects event encodings and capabilities)."""
        ver = max(1, min(0xFF, int(version)))
        response = self._send_command(self.CMD_SSP_HOST_PROTOCOL_VERSION, bytes([ver]))
        ok = bool(response and response[2] == self.RSP_SSP_OK)
        if ok:
            self._status(f"Host Protocol Version set to {ver}.")
        return ok

    def get_last_reject_reason(self) -> Optional[str]:
        """Query the device for the last reject reason (separate from POLL events)."""
        response = self._send_command(self.CMD_SSP_LAST_REJECT_CODE)
        if response and len(response) >= 4 and response[2] == self.RSP_SSP_OK:
            reason_code = response[3]
            return self.REJECT_REASONS.get(reason_code, f"0x{reason_code:02X}")
        return None

    def _set_inhibits(self, inhibits: Optional[bytes] = None) -> bool:
        """Enable/disable channels.

        Args:
            inhibits: Bitmask bytes, LSB = channel 1. 1 = enabled, 0 = inhibited.
                      If None, enables all known channels with a sensible default width.
        """
        if inhibits is None:
            # Default to all channels enabled. If we don't know, assume 16 (two bytes).
            total_channels = self.num_channels or 16
            num_bytes = max(1, (total_channels + 7) // 8)
            num_bytes = max(num_bytes, 2)  # NV9 commonly expects at least 2 bytes
            mask = bytearray([0xFF] * num_bytes)
            if self.num_channels and (self.num_channels % 8):
                # Trim spare bits in final byte when we *do* know the exact channel count
                used_bits = self.num_channels % 8
                mask[-1] &= (1 << used_bits) - 1
            inhibits = bytes(mask)

        response = self._send_command(self.CMD_SSP_SET_INHIBITS, inhibits)
        return bool(response and response[2] == self.RSP_SSP_OK)

    def _toggle_sequence_bit(self):
        """Alternates the SSP sequence bit between 0x00 and 0x80 after a good response."""
        self.sequence_bit = 0x80 if self.sequence_bit == 0x00 else 0x00

    def _read_full_response(self, deadline_s=0.5):
        """
        Read one complete SSP response frame and return the unstuffed payload bytes
        (addr|seq, length, status/code, params...) with the CRC removed.

        Wire format
        -----------
        On the line, responses are encoded as:
            [STX]  <byte-stuffed: payload || CRC16-LE>
        where:
            payload := [addr|seq][len][code][params...]
            CRC16   := CRC-16 over 'payload' with poly 0x8005, seed 0xFFFF.
        Any literal 0x7F bytes inside 'payload||CRC' are byte-stuffed as 0x7F 0x7F.

        Parameters
        ----------
        deadline_s : float
            Absolute time budget to collect a full frame before giving up.

        Returns
        -------
        bytes | None
            The unstuffed payload (without CRC) on success, or None on timeout/CRC failure.
        """
        # Must have an open port to read from.
        if not (self.serial_port and self.serial_port.is_open):
            return None

        deadline = time.time() + deadline_s

        # Buffer of the *stuffed* bytes after the initial STX (0x7F).
        stuffed_after_stx = bytearray()
        saw_stx = False

        while time.time() < deadline:
            # Read whatever is currently available to minimize system call overhead.
            if self.serial_port.in_waiting:
                to_read = max(1, self.serial_port.in_waiting)
                chunk = self.serial_port.read(to_read)

                for byte in chunk:
                    if not saw_stx:
                        # Scan for start-of-frame marker.
                        if byte == self.STX:
                            saw_stx = True
                            stuffed_after_stx.clear()
                        # else: ignore noise before STX
                        continue

                    # Accumulate stuffed bytes after STX.
                    stuffed_after_stx.append(byte)

                    # Attempt to unstuff and see if we already have a complete frame.
                    # (This is cheap for short buffers; it keeps the loop simple.)
                    unstuffed = self._unstuff_bytes(stuffed_after_stx)

                    # Need at least 3 bytes of payload to know the declared length:
                    #   [addr|seq][len][code]
                    if len(unstuffed) >= 3:
                        declared_len = unstuffed[1]                  # number of bytes after 'len' (code + params)
                        total_payload_no_crc = 2 + declared_len      # addr|seq + len + (code+params)
                        total_payload_with_crc = total_payload_no_crc + 2  # plus CRC16-LE

                        # Guard against pathological length values that would overflow our buffer.
                        # (We don’t have a spec max here; this just prevents runaway parsing.)
                        if declared_len < 1:
                            # len must at least include the 'code' byte
                            return None

                        # If we’ve received enough unstuffed bytes to include the CRC, verify it.
                        if len(unstuffed) >= total_payload_with_crc:
                            payload = unstuffed[:total_payload_no_crc]
                            crc_rx = unstuffed[total_payload_no_crc:total_payload_with_crc]
                            crc_calc = self._calculate_crc(payload)

                            # Return payload on a clean CRC; otherwise treat as a framing error.
                            return payload if crc_rx == crc_calc else None

                # Continue outer while loop to check time and/or read more bytes.
            else:
                # Nothing available right now; yield briefly to avoid busy-waiting.
                time.sleep(0.001)

        # Timed out before a full, CRC-valid frame was assembled.
        return None


    def _send_sync_once(self):
        """Write a single SYNC and read its response (no retries)."""
        packet = self._build_packet(self.CMD_SSP_SYNC, b'')
        try:
            self.serial_port.write(packet)
        except serial.SerialTimeoutException:
            return None
        return self._read_full_response(deadline_s=self.READ_TIMEOUT_S)

    def _send_command(self, command, params=b'', retries=2):
        """
        Build and transmit one SSP command, wait for the reply, and manage the sequence bit.

        Control flow (happy path)
        -------------------------
        1) Build frame: [STX][stuffed( addr|seq, len, command, params, CRC16 )]
        2) Write frame to the serial port.
        3) Read one response frame (CRC-checked) within a deadline.
        4) Verify address/seq (best-effort), toggle our sequence bit, and return the payload.

        Error handling
        --------------
        - On write timeout: retry (up to `retries`) then give up.
        - On CRC/framing/timeout: after final attempt, do one SYNC-based recovery round:
            * Flush I/O buffers, issue SYNC, force next seq=0 if SYNC returns OK,
            then resend the original command once and read again.
        - If anything fails, return None.

        Returns
        -------
        bytes | None
            The unstuffed payload (addr|seq, len, code, params...) on success, else None.
        """
        # Must have a port to talk to.
        if not (self.serial_port and self.serial_port.is_open):
            self._error("Serial port is not open.")
            return None

        # Pick a deadline: Setup replies can be larger/slower.
        def _deadline_for(cmd: int) -> float:
            return self.SETUP_DEADLINE_S if cmd == self.CMD_SSP_SETUP_REQUEST else self.DEFAULT_DEADLINE_S

        # Small helper to sanity-check the reply header and toggle seq on success.
        def _finalize_and_return(payload: bytes) -> bytes:
            # Defensive: verify that reply came from the expected address and with the expected seq bit.
            addr_seq = payload[0]
            reply_addr = addr_seq & 0x7F
            reply_seq  = addr_seq & 0x80
            expected_addr = self.slave_id & 0x7F
            expected_seq  = self.sequence_bit & 0x80

            if reply_addr != expected_addr:
                self._status(f"Warning: reply address {reply_addr:#04x} != expected {expected_addr:#04x}")
            if reply_seq != expected_seq:
                # A mismatch can happen on retransmits; we just warn and continue.
                self._status("Warning: reply sequence bit mismatch (possible retransmit).")

            # Happy path: we got a reply—flip our sequence bit for the next command.
            self._toggle_sequence_bit()
            return payload

        # ---- Primary attempt(s) -------------------------------------------------
        for attempt in range(retries + 1):
            try:
                # Build and transmit the command packet.
                packet = self._build_packet(command, params)
                self.serial_port.write(packet)
            except serial.SerialTimeoutException:
                # Write timed out—retry unless we’re out of attempts.
                if attempt == retries:
                    return None
                continue  # next attempt

            # Read one full response (CRC verified) within the appropriate deadline.
            payload = self._read_full_response(deadline_s=_deadline_for(command))
            if payload is not None:
                return _finalize_and_return(payload)

            # If response was invalid/timeout, loop to next attempt (unless we’re at the last try).
            # The recovery (SYNC) happens *after* the loop if we exhausted attempts.

        # ---- Recovery path: one SYNC-based reset + resend -----------------------
        # Save current seq so we can restore it on total failure.
        old_seq = self.sequence_bit

        # Clear buffers to drop any garbage that might confuse SYNC or the resend.
        try:
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()
        except Exception:
            pass
        time.sleep(0.005)

        # Try a one-off SYNC to realign framing/sequence on both ends.
        sync_payload = self._send_sync_once()
        if sync_payload is not None and len(sync_payload) >= 3 and sync_payload[2] == self.RSP_SSP_OK:
            # After a successful SYNC, the next command *must* be sent with seq=0.
            self.sequence_bit = 0x00
            try:
                # Resend the original command once more after SYNC.
                packet2 = self._build_packet(command, params)
                self.serial_port.write(packet2)

                payload = self._read_full_response(deadline_s=self.DEFAULT_DEADLINE_S)
                if payload is not None:
                    return _finalize_and_return(payload)

            except serial.SerialTimeoutException:
                # If we can’t write even after SYNC, we’re done.
                pass

        # SYNC recovery didn’t yield a usable reply—restore the previous sequence state.
        self.sequence_bit = old_seq
        return None


    def _process_events(self, data: bytes) -> List[NV9Event]:
        events: List[NV9Event] = []
        idx = 0
        while idx < len(data):
            code = data[idx]
            idx += 1

            if code in (self.RSP_SSP_NOTE_READ, self.RSP_SSP_CREDIT_NOTE):
                if idx < len(data):
                    ch = data[idx]
                    idx += 1
                else:
                    ch = 0
                if ch == 0:
                    events.append(NV9Event(code, "READING"))
                else:
                    name = "NOTE_READ" if code == self.RSP_SSP_NOTE_READ else "CREDIT"
                    val = self.channel_value_map.get(ch) or self.euro_values.get(ch)
                    events.append(NV9Event(code, name, channel=ch, value=val))

            elif code == self.RSP_SSP_REJECTED:
                events.append(NV9Event(code, "REJECTED"))
                
            elif code == self.RSP_SSP_REJECTING:
                events.append(NV9Event(code, "REJECTING")) 

            elif code == self.RSP_SSP_STACKING:
                events.append(NV9Event(code, "STACKING"))

            elif code == self.RSP_SSP_STACKED:
                events.append(NV9Event(code, "STACKED"))

            elif code == self.RSP_SSP_DISABLED:
                events.append(NV9Event(code, "DISABLED"))

            elif code == self.RSP_SSP_SLAVE_RESET:
                events.append(NV9Event(code, "SLAVE_RESET"))

            else:
                events.append(NV9Event(code, "UNKNOWN"))

        return events

    # --- statics ---
    @staticmethod
    def _calculate_crc(data: bytes) -> bytes:
        crc, poly = 0xFFFF, 0x8005
        for byte in data:
            crc ^= (byte << 8)
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
        return struct.pack('<H', crc)

    @staticmethod
    def _unstuff_bytes(data: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == NV9Validator.STX and i + 1 < len(data) and data[i + 1] == NV9Validator.STX:
                out.append(NV9Validator.STX)
                i += 2
            else:
                out.append(data[i])
                i += 1
        return bytes(out)

    def _build_packet(self, command_code, params=b''):
        length = len(params) + 1
        # First byte is (address|seq): low 7 bits = slave address, high bit = sequence
        addr_seq = (self.slave_id & 0x7F) | (self.sequence_bit & 0x80)
        payload = bytes([addr_seq, length, command_code]) + params
        crc = self._calculate_crc(payload)
        stuffed = bytearray([self.STX])
        for b in payload + crc:
            if b == self.STX:
                stuffed += bytes([self.STX, self.STX])
            else:
                stuffed.append(b)
        return bytes(stuffed)

    # --- small helpers ---
    def _status(self, msg: str):
        if self.on_status:
            self.on_status(msg)

    def _error(self, msg: str):
        if self.on_error:
            self.on_error(msg)