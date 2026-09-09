import platform
import subprocess
import sys


def main():
    print(f"Building ATOMIQA for {platform.system()}...")

    # PyInstaller uses ';' as a separator for --add-data on Windows, and ':' on Unix-like systems
    data_separator = ";" if platform.system() == "Windows" else ":"

    data_args = [
        f"data{data_separator}data",
        f"hardware_library{data_separator}hardware_library",
        f"examples{data_separator}examples",
    ]

    # Build the PyInstaller command arguments
    # Note: --noconfirm automatically overwrites the dist/ folder if it exists
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--icon=app_icon.ico",
        "--name=ATOMIQA",
        "--collect-all=PyQt6",
        "--collect-all=numpy",
        "--collect-all=pyqtgraph",
        *[f"--add-data={arg}" for arg in data_args],
        "app.py",
    ]

    print("Executing command:")
    print(" ".join(args))
    print("-" * 50)

    try:
        # Run PyInstaller
        subprocess.run(args, check=True)
        print("\n" + "=" * 50)
        print("Build completed successfully!")
        print("Your executable is located in the 'dist/ATOMIQA' folder.")
    except subprocess.CalledProcessError as e:
        print("\n" + "=" * 50)
        print(
            "Build failed! Make sure PyInstaller is installed ('pip install pyinstaller')."
        )
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
