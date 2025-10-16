import os
import sys
import shutil

# --- Configuration ---
# The relative path where you've stored the DLL, starting from the project root.
# This matches the folder structure from your screenshot.
SOURCE_DLL_PATH = os.path.join('libusb', 'MinGW64', 'dll', 'libusb-1.0.dll')

def main():
    """Finds and copies the libusb DLL to the active venv's Scripts folder."""
    
    # Get the root directory of the project (where this script is run from)
    project_root = os.getcwd()
    
    # 1. Define the full source and destination paths
    source_file = os.path.join(project_root, SOURCE_DLL_PATH)
    
    # Use sys.prefix to reliably find the root of the active virtual environment
    # On Windows, the executables are in the 'Scripts' subfolder.
    dest_dir = os.path.join(sys.prefix, 'Scripts')
    dest_file = os.path.join(dest_dir, os.path.basename(SOURCE_DLL_PATH))

    print(f"Source file:      {source_file}")
    print(f"Destination folder: {dest_dir}")
    print("-" * 50)

    # 2. Check if the source file exists
    if not os.path.exists(source_file):
        print(f"❌ ERROR: Source DLL not found at '{source_file}'")
        print("Please ensure the 'libusb' folder is structured correctly in your project.")
        sys.exit(1)
    
    # 3. Check if the destination directory exists
    if not os.path.isdir(dest_dir):
        print(f"❌ ERROR: Virtual environment 'Scripts' folder not found.")
        print("Please make sure you are running this from an active virtual environment.")
        sys.exit(1)

    # 4. Check if the file is already in the destination
    if os.path.exists(dest_file):
        print(f"✅ INFO: DLL already exists in the destination. No action needed.")
        sys.exit(0)

    # 5. Copy the file
    try:
        print("Attempting to copy the DLL...")
        shutil.copy2(source_file, dest_dir)
        print(f"✅ SUCCESS: '{os.path.basename(SOURCE_DLL_PATH)}' copied to the Scripts folder.")
    except Exception as e:
        print(f"❌ ERROR: Failed to copy file. An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()