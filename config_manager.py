import json
import sys

class ConfigManager:
    """A simple class to load and manage application configuration from a JSON file."""
    def __init__(self, config_path='config.json'):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error: Could not load or parse {config_path}. {e}")
            print("Please ensure 'config.json' exists and is correctly formatted.")
            sys.exit(1)

        # Load all values from the JSON file
        self.kiosk_id = config_data.get('kiosk_id')
        # 1. Read the new URL template
        starting_url_template = config_data.get('starting_url')
        heartbeat_endpoint_template = config_data.get('heartbeat_endpoint')
        self.preshared_key = config_data.get('preshared_key')

        # Validate that essential templates and keys are present
        required_keys = [starting_url_template, self.preshared_key, self.kiosk_id]
        if not all(required_keys):
            print("Error: 'starting_url', 'preshared_key', and 'kiosk_id' are required in config.json.")
            sys.exit(1)
            
        # 2. Create the final, formatted URLs
        self.starting_url = starting_url_template.format(self.kiosk_id)
        self.heartbeat_url = heartbeat_endpoint_template.format(self.kiosk_id)