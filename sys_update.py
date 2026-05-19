#!/usr/bin/env python3
import subprocess
import sys
import os
import getpass
import ctypes
import stat
import pwd
import pexpect
from datetime import datetime
from nacl.secret import SecretBox
from nacl.utils import random as nacl_random

LOG_FILE = "/var/log/sys_update.log"

# ──────────────────────────────────────────────
# Memory locking — prevents swap to disk
# ──────────────────────────────────────────────
def mlock(data: bytearray):
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    addr = (ctypes.c_char * len(data)).from_buffer(data)
    if libc.mlock(addr, len(data)) != 0:
        print("[WARN] mlock failed — memory may be swappable")

def munlock(data: bytearray):
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    addr = (ctypes.c_char * len(data)).from_buffer(data)
    libc.munlock(addr, len(data))

def secure_wipe(data: bytearray):
    munlock(data)
    for i in range(len(data)):
        data[i] = 0

# ──────────────────────────────────────────────
# Encrypted password vault (lives in RAM only)
# ──────────────────────────────────────────────
class SecurePassword:
    def __init__(self, plaintext: str):
        self._key = bytearray(nacl_random(SecretBox.KEY_SIZE))
        mlock(self._key)

        plain_bytes = bytearray(plaintext.encode())
        mlock(plain_bytes)

        box = SecretBox(bytes(self._key))
        encrypted = box.encrypt(bytes(plain_bytes))

        self._blob = bytearray(encrypted)
        mlock(self._blob)

        secure_wipe(plain_bytes)
        del plain_bytes, plaintext

    def decrypt(self) -> bytearray:
        box = SecretBox(bytes(self._key))
        plain = bytearray(box.decrypt(bytes(self._blob)))
        mlock(plain)
        return plain

    def wipe(self):
        secure_wipe(self._key)
        secure_wipe(self._blob)
        del self._key, self._blob

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(entry)
    print(entry, end="")

# ──────────────────────────────────────────────
# Sudo check
# ──────────────────────────────────────────────
def check_sudo():
    if os.geteuid() != 0:
        print("Please run with sudo\n")
        sys.exit(1)
    log("Running as root: OK")

# ──────────────────────────────────────────────
# pexpect — auto-respond to password prompts
# ──────────────────────────────────────────────
def run_paru_with_secure_password(real_user: str, secure_pwd: SecurePassword):
    plain: bytearray = None
    password_str: str = None

    try:
        plain = secure_pwd.decrypt()
        password_str = plain.decode()

        # Wipe plain immediately after decode
        secure_wipe(plain)
        plain = None

        log("paru is running")

        child = pexpect.spawn(
            "sudo", ["-u", real_user, "-i", "--", "paru", "-Sua", "--noconfirm", "--sudoloop"],
            encoding="utf-8",
            timeout=600
        )

        # Stream paru output live to terminal
        child.logfile_read = sys.stdout

        while True:
            index = child.expect([
                r"\[sudo\] password for .+:",  # sudo prompt
                r"Password:",                   # fallback prompt
                pexpect.EOF,
                pexpect.TIMEOUT
            ], timeout=600)

            if index in (0, 1):
                child.sendline(password_str)   # auto-paste password
            elif index == 2:
                break                           # done
            elif index == 3:
                log("[ERROR] Timeout waiting for paru")
                child.close(force=True)
                sys.exit(1)

        child.close()

        if child.exitstatus != 0:
            log(f"[ERROR] paru exited with code {child.exitstatus}")
            sys.exit(1)

        log("paru done")

    except Exception as e:
        log(f"[ERROR] paru failed: {e}")
        sys.exit(1)

    finally:
        # Wipe password from memory
        if plain:
            secure_wipe(plain)
        if password_str:
            del password_str

# ──────────────────────────────────────────────
# Main update flow
# ──────────────────────────────────────────────
def update(secure_pwd: SecurePassword):
    real_user = os.environ.get("SUDO_USER")
    if not real_user:
        log("[ERROR] Could not detect original user.")
        sys.exit(1)

    try:
        log("pacman is running")
        subprocess.run("pacman -Syu --noconfirm", shell=True, check=True)
        log("pacman done")

        log("flatpak is running")
        subprocess.run(
            f"su - {real_user} -c 'flatpak update -y'",
            shell=True, check=True
        )
        log("flatpak done")

        run_paru_with_secure_password(real_user, secure_pwd)

        log("All updates completed successfully")

    except subprocess.CalledProcessError as e:
        log(f"[ERROR] Command failed: {e}")
        sys.exit(1)

    finally:
        secure_pwd.wipe()
        log("Password vault wiped from memory")

# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
check_sudo()

raw_password = getpass.getpass(prompt="[sudo] password (encrypted in RAM): ")
secure_pwd = SecurePassword(raw_password)
del raw_password

update(secure_pwd)

