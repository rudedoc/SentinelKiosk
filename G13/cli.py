from .g13_validator import G13Validator
# -------------
# Minimal usage
# -------------
if __name__ == "__main__":
    g = G13Validator(port="COM8", addr=2).open()
    try:
        g.status()
        # Optional: set sorter mapping 1:1 for the first five types
        g.set_sorter_paths([1, 2, 3, 4, 5])
        print("Polling credits... (Ctrl+C to quit)")
        g.enable_and_poll()  # prints events by default
    finally:
        g.close()