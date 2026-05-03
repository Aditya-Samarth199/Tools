#!/usr/bin/env python3
import subprocess
import sys 
import os
from datetime import datetime

LOG_FILE = "/var/log/sys_update.log"

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(entry)
    print(entry, end="")

def check_sudo():
    if os.geteuid() != 0:
        print("Please run with sudo \n")
        exit()
    else:
        log("sudo is on")

def update():
    real_user = os.environ.get("SUDO_USER")

    if not real_user:
        log("[ERROR] Could not detect the original user. Run with sudo.")
        sys.exit(1)

    try:
        log("pacman is running")
        subprocess.run("pacman -Syu", shell=True, check=True)
        log("pacman done")

        log("flatpak is running")
        subprocess.run(f"su - {real_user} -c 'flatpak update -y'", shell=True, check=True)
        log("flatpak done")

        log("yay is running")
        subprocess.run(f"su - {real_user} -c 'yay -Syu --noconfirm --answerdiff=None --answerclean=None --sudoloop'", shell=True, check=True)
        log("yay done")

        log("All updates completed successfully")

    except subprocess.CalledProcessError as e:
        log(f"[ERROR] Command failed: {e}")
        sys.exit(1)

check_sudo()
update()

