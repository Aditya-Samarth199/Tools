import subprocess

def Password_gen():
    length = int(input("Enter number: "))
    if length > 500:
        print("please enter smaller value")
    
    else:
        result = subprocess.run(f"head -c {length} /dev/urandom | base64", shell=True, capture_output=True, text=True)

    password = result.stdout.strip()
    print(password)

Password_gen()
