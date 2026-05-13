import subprocess
from datetime import datetime
import os
import sys

LOG_FILE = "/var/log/scan.log"

def check_sudo():
    if os.geteuid() != 0:
        print("Please run with sudo\n")
        sys.exit(1)
    print("Running as root: OK")

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(entry)
    print(entry, end="")


def scanning_tool(flag_list, dom_list):
    print("scanning tool is running")
    
    scan = subprocess.run(["nmap"] + flag_list + dom_list, capture_output=True, text=True)
    log(f"nmap {' '.join(flag_list)} {' '.join(dom_list)}")
    print(scan.stdout)

flag_list = [] # to store all flags in list for scan
dom_list = [] # to store all domain in list for scan

while True:
    user_dom = input("write domain you want to scan: ")
    dom_list.append(user_dom)

    yn1 = input("Do you want to add more dom [y/n]: ").lower() # if user want to add more domains
    if yn1 == 'n':
        break

while True:
    user_flag = input("write flag you want to apply: ")
    flag_list.append(user_flag)

    yn2 = input("do you want to add more flags [y/n]: ").lower() # if user want to add more flags
    if yn2 == 'n':
        break

check_sudo()
scanning_tool(flag_list, dom_list)

