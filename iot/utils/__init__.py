

def return_hex(data):
    return bytearray.fromhex("".join(data))

def return_mac(value):
    return "".join(format(x, '02x') for x in reversed(value))
