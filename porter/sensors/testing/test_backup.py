with open('multitasking.txt', 'a') as file:
    for i in range(10000):
        file.write('test')
