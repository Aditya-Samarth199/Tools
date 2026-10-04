import math

number = int(input("Enter your number: "))
i = [2, 3, 5, 7]
if number < 2:
    for n in range(5):
        if number % i[0] == 0:
            print("number is not prime")
        else:
            print("number is prime")
else:
    print("number is not prime")
