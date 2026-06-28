import requests
import argparse
import concurrent.futures
import threading

urls = {"url1": "https://thehackernews.com/"}
username = input("Enter your username you want to find: ")

respond = requests.get(urls["url1"])

print(respond)
