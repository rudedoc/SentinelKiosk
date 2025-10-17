import json
import sys

class KioskConfig:
    """A simple class to load and manage application configuration from a JSON file."""
    def __init__(self, config_path='config.json'):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error: Could not load or parse {config_path}. {e}")
            print("Please ensure 'config.json' exists and is correctly formatted.")
            sys.exit(1)

        self.user_id = config_data.get('user_id')
        self.starting_url = config_data.get('starting_url')
        self.heartbeat_url = config_data.get('heartbeat_endpoint')
        self.preshared_key = config_data.get('preshared_key')
        self.brand_name = config_data.get('brand_name')
        self.logo_path = config_data.get('logo_path')

        printer_config = config_data.get('printer')
        self.printer_mock = printer_config.get('mock')
        self.printer_vendor_id = printer_config.get('vendor_id')
        self.printer_product_id = printer_config.get('product_id')
        self.printer_interface = printer_config.get('interface')
        self.printer_in_endpoint = printer_config.get('in_endpoint') # 0x81
        self.printer_out_endpoint = printer_config.get('out_endpoint') # 0x03


        # ---- nv9 block (this is what you asked to add) ----
        nv9 = config_data.get("nv9")
        # keep names close to JSON keys for clarity
        self.nv9_port_name: str = nv9.get("port_name")
        self.nv9_baud_rate: int = int(nv9.get("baud_rate"))
        self.nv9_slave_id: int = int(nv9.get("slave_id"))
        self.nv9_host_protocol_version: int = int(nv9.get("host_protocol_version"))

        g13 = config_data.get("g13")
        self.g13_port_name: str = g13.get('port_name')
        self.g13_address: int = g13.get('address')

        # Validate that essential templates and keys are present
        required_keys = [self.starting_url, self.preshared_key, self.user_id]
        if not all(required_keys):
            print("Error: 'starting_url', 'preshared_key', and 'user_id' are required in config.json.")
            sys.exit(1)
            
    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "starting_url": self.starting_url,
            "heartbeat_url": self.heartbeat_url(),
            "preshared_key": self.preshared_key,
            "brand_name": self.brand_name,
            "logo_path": self.logo_path,
            "printer": {
                "mock": self.printer_mock,
                "vendor_id": self.printer_vendor_id,
                "product_id": self.printer_product_id,
                "interface": self.printer_interface,
                "in_endpoint": self.printer_in_endpoint,
                "out_endpoint": self.printer_out_endpoint,
            },
            "nv9": {
                "port_name": self.nv9_port_name,
                "baud_rate": self.nv9_baud_rate,
                "slave_id": self.nv9_slave_id,
                "host_protocol_version": self.nv9_host_protocol_version,
            },
        }