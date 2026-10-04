import requests
import argparse
import concurrent.futures
import threading

urls = {"url1": "https://thehackernews.com/",
    "url2": "https://github.com/",
    "url3": "https://gitlab.com"}
username = input("Enter your username you want to find: ")

n = 1
for i in range (1, 3):
    n +=1 
    af = str(n)
    us = "url" + af
    respond = requests.get(urls[us])


print(respond)
