import logging

from iot.server import server


# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)


# List of possible commands
PING, LIST, STATUS, ON, OFF, DEVICE = range(6)


DAIKIN_AIRCON_OFF = "&\x00P\x02\x0e\x0f\x0f\r\x0e\x0e\r\x11\x0e\x0f\x0b\x00\x03Es9\r-\x0f\x0f\x0b\x10\r\x10\x0c+\x0e\x0f\x0e\x10\r\x0e\r\x10\x11'\x10\r\r,\x0c,\x0e\x0f\r,\x0e*\r,\r-\x0f)\x0f\x0e\r\x0f\r,\x0e\x0e\r\x10\r\x10\x0c\x10\x0e\x0e\x10\x0c\r\x10\x0c\x10\r\x0f\x0e\x10\x0e*\x0f\r\x0f*\r\x0f\r\x10\r\x10\r+\x0e,\x0f\r\x0c\x10\r\x0f\r\x11\x0c\x10\r\x0f\x0e\x10\r\x0e\x0f\r\r\x0f\r\x11\x0c\x10\x0c\x10\x0e\x10\x0c\x0f\x0e\x0f\x0c,\x0e+\r,\x0e\x0f\x0e*\x0e\x0e\r,\x0e+\x0e\x00\x04}q9\r,\x0e\x0f\x0e\x10\x0b\x0f\x10)\x0e\x10\x0c\x0f\x0e\x0f\x0e\x11\x0b,\r\x0e\x0e+\x0e+\r\x10\x0e*\x10)\r,\r,\r,\x0e\x0e\x10\x0e\x0c,\r\x0f\x0e\x0f\x0f\x0f\x0b\x10\x0e\x0f\x0b\x10\x0e\x10\x0c\x10\x0c\x10\x0e\x0f\x0e\x0e\x0e*\x0f\x0f\x0c\x0f\x10\r\r\x10\x0e+\x0c\x0f\x10)\x10\r\r\x0f\r\x10\r,\x0f\x0f\x0c\x0e\x10)\x0e\x10\r\x0e\r\x11\x0c\x10\r\x0f\x0f\x0e\x0c\x0f\x0f\x0f\r,\r\x0e\r-\x0c\x0f\x11\r\r+\r,\x0e*\x12\x00\x04yq:\r+\x0f\x0f\x0c\x0f\x0f\x0e\x0e+\x0f\r\r\x10\r\x11\x0b\x10\x0e*\x10\x0e\x0c,\r+\x0f\x0f\x0c,\x0e+\x0e*\x0f*\x0f*\x12\x0c\x0c\x0f\r,\r\x10\x0e\x0f\x0c\x0f\x10\x0e\x0c\x0f\x0e\x10\r\x0e\x11\x0c\r\x10\x0b\x11\x0f\x0e\x0c\x0f\x10\x0e\r\x0e\x10\r\r\x0f\x10\x0c\x0f\x10\x0b\x0f\x0f\x0f\r\x0e\x11(\r,\x0c-\r\x0f\x11\r\x0c\x0f\x0f\x0e\r+\x0e,\x0e\x0f\r+\x0c\x10\x0f\x0e\r\x0f\r\x10\x0f\x0f\x0b\x10\x0f\x0f\x0c\x0e\x0c\x11\r\x0f\x10)\r,\x0e+\x0f)\x10*\r\x0f\x0e+\x0e\x0e\x0e\x0f\r\x0f\r\x11\x0c\x10\x0b\x11\x10\r\x0b\x10\x0f\x0e\x0c\x10\x12\x0b\x0e\x0e\r\x10\x0f\x0f\x0b\x11\x10\x0c\x0c\x0f\x10\r\r,\r,\x0f\x0e\r\x0f\x0f\x0e\x0c\x0f\x0f\x0e\x0e\x0e\x0c\x12\x0e\x0f\x0b\x0f\x10\x0e\x0b-\x0e+\r\x0f\x0c\x11\x10\r\x0c\x0f\x10\r\x0e\x0e\x10\r\x0c\x10\x0c\x10\x0f\x11\t\x11\x0e\x0e\r\x0f\x10\r\r\x0f\r\x0f\x0e\x0f\r\x10\x0f\x10\n\x0f\x10\r\r\x0f\x10\x0e\x0c+\x0f*\x10\x0e\x0c,\x0f\r\x12\x0b\r\x0f\x0c\x11\x0e\x10\x0b\x10\x0f\x0e\x0c,\r\x0f\x0e\x0f\r\x11\x0b\x10\x0e\x11\t\x11\x11'\r,\r,\x0c-\x0e+\x0f)\x0f*\x0f+\x10\x00\r\x05\x00\x00\x00\x00\x00\x00\x00\x00"
DAIKIN_AIRCON_ON = "&\x00P\x02\x10\x0e\x0e\x0e\x0f\r\x0f\x0e\x0f\x0e\x0e\x00\x03Ds8\x0f+\x0e\x0e\x0e\x0e\x0f\x0e\x0e*\x0f\r\x10\x0e\x0e\x0e\x0f\r\x0f*\x0f\x0e\x0e*\x0f*\x0f\r\x0f+\x0e*\x0f*\x0f*\x0f)\x10\x0e\x0e\x0e\x0f)\x10\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e)\x10\x0e\x0e*\x0f\x0e\x0f\x0e\x0e\x0e\x0e*\x0f*\x0f\r\x10\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\r\x0f\x0e\x0f\x0e\x0e\x0e\x0f\r\x0f*\x0f*\x0f)\x10\x0e\x0e+\x0e\x0e\x0f*\x0e*\x0f\x00\x04{s8\x0f*\x0f\r\x10\x0e\x0e\x0e\x0f)\x0f\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e*\x0f\r\x0f+\x0e*\x0f\x0e\x0f)\x10)\x0f*\x0f+\x0e*\x0f\x0e\x0e\x0e\x0f)\x10\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\r\x10)\x0f\x0f\x0e\x0e\x0e\x0e\x0f\x0e\x0e*\x0f\x0e\x0f)\x0f*\x0f*\x0f\x0e\x0f\x0e\x0e\x0e\x0e\x0f\x0e)\x10\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e+\x0e*\x0f\x0e\x0f)\x0f+\x0e\x0e\x0f)\x10*\x0e\x00\x04|r8\x0f*\x0f\x0e\x0f\x0e\x0e\x0e\x0f)\x10\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e+\x0e\x0e\x0f)\x0f*\x0f\r\x10)\x10)\x0f*\x0f*\x0f*\x0f\r\x10\x0e\x0e)\x10\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\r\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\r\x10\x0e\x0e\x0e\x0f\x0e\x0e*\x0f)\x10)\x10\x0e\x0e\x0e\x0f\r\x0f\x0e\x0e*\x0f+\x0e\x0e\x0f)\x10\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f*\x0f)\x10)\x10)\x0f+\x0e\r\x10*\x0f\x0e\x0e\r\x0f\x0e\x0f\x0e\x0e\x0e\x0f\r\x0f\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\r\x10\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e*\x0f*\x0f\x0e\x0f\r\x0f\r\x10\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\r\x10)\x0f*\x0f\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\r\x10\r\x0f\x0e\x0f\x0e\x0e\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0f\r\x0f\x0e\x0f\x0e\x0e\x0e\x0f\x0e\x0e\x0e\x0e*\x0f+\x0e\x0e\x0f)\x0f\x0e\x0f\x0e\x0e\r\x10\x0e\x0e\x0e\x0f\x0e\x0e\r\x10)\x0f\x0e\x0f\r\x0f\x0e\x0f\x0e\x0e\r\x10\r\x0f)\x10)\x10)\x10*\x0f)\x0f+\x0e*\x0f*\x0f\x00\r\x05\x00\x00\x00\x00\x00\x00\x00\x00"

DAIKIN_AIRCON_ON = "26005002100e0e0e0f0e0e0e0f0e0e00034473380f2a0f0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e2a0f0e0e2a0f2a0f0e0f290f2a0f2a0f2a0f2a0f0e0e0f0e2a0f0e0e0e0f0e0e0e0f0e0e0e0e0f0e0e0e0e0f0e0e2a0f0e0f290f0f0e0e0e0e0f291029100e0e0e0e0f0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f290f2a0f2a0f0e0f29100e0e2a0f2a0f00047b73380f2a0f0e0e0e0f0e0e2a0f0e0f0e0e0e0e0f0e2a0f0e0e2a0f2a0f0e0e2a0f2a0f2a0f2a0f2a0f0e0e0e0f29100e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e2a0f0e0e2a0f0e0f29102910290f0f0e0e0e0e0f29100e0e2a0f0e0e0e0f0e0e0e0f0e0e0e0f2910290f0e0f2910291029100e0e00047b73380f2a0f0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e2a0f0e0e2b0e2a0f0e0f290f2a0f2a0f2a0f2a0f0e0e0f0e29100e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f29100e0e0e0f290f2a0f2a0f0e0f0e0e0e0f0e0e2a0f29100e0e2a0f0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e2a0f2a0f2a0f2a0f2a0f0e0e2a0f0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e2a0f2b0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0d0f0e0e2a0f2a0f0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0d0f0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e2a0f2a0f0e0f290f0e0f0e0e0e0f0e0e0e0f0e0e0e0e2a0f0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0d0f0e0f0e0e0e0f000d050000000000000000"
DAIKIN_AIRCON_OFF = "26005002100d0f0d0f0d100d0f0d1000034473371029100d0f0d0f0d1029100d0f0d100d0f0d10290f0d10291029100d0f291029102910290f2a0f0d100d0f2a0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f2a0f0d1029100d0f0d100d0f291029100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f0d102910290f2a0f0d1029100d0f2a0f291000047b73371029100d0f0d100d0f29100d0f0d100d0f0d10290f0e0f291029100d0f2a0f291029102910290f0e0f0d0f2a0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f29100d0f0d100d0f0d1029100d0f29100d10290f0d100d0f0d0f2a0f2a0f0d100d0f0d100d0f0d0f0d100d0f0d1029100d0f0d0f2a0f2a0f0d100d0f0d1000047a733810290f0d100d0f0d1029100d0f0d0f0d100d0f2a0f0d10290f2a0f0d1029102910290f2a0f2a0f0d100d0f29100d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f2910291029100d0f0d100d0f0d0f2a0f2a0f0d1029100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d10291029102910290f2a0f0d1029100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f0d100d0f0d0f2a0f2a0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d10290f2a0f0d100d0f0d100d0f0d0f0e0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f0d100d0f0d100d0f0d0f0d100d0f2a0f2a0f0d10290f0d100d0f0d100d0f0d100d0f0d0f2a0f0d100d0f0d100d0f0d100d0f291029102910290f2a0f2a0f2a0f2a0f000d050000000000000000"

DAIKIN_AIRCON_POWERFUL ="260050020f0e0f0e0e0e0f0e0e0e0e000345723810290f0e0f0e0e0e0f29100e0e0e0f0e0e0e0e2a0f0e0f291029100e0e2a0f291029102910290f0e0f0e0e2a0f0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f29100d0f29100e0e0e0f0e0e2a0f2a0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e2a0f291029100e0e2a0f0e0e2a0f2b0e00047b73380f2a0f0e0f0e0e0e0f2a0f0e0e0e0e0e0f0e0e2a0f0e0f2910290f0e0f291029102910290f2a0f0e0f0e0e2a0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f29100e0e0e0f0e0e0e0f290f0e0f0e0e2a0f2a0f2a0f2a0f0e0e0e0f0e0e2a0f0e0f290f0e0f0e0e0e0f0e0e0e0f2910290f2a0f0e0f29102910290f0e0f00047b73380f2a0f0e0e0e0f0e0e2a0f0e0f0e0e0e0e0e0f29100e0e2a0f2a0f0e0e2a0f2a0f2a0f2a0f2a0f0e0e0e0f29100e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f290f0f0e0e0e2a0f2a0f2a0f0e0e0e0f0e0e0e0f291029100e0e2a0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e2a0f2a0f2a0f291029100e0e2a0f0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e2a0f29100e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e2a0f2a0f0e0e2a0f0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0e0e0f0e0e0e0f0e0e0e0f290f2a0f0e0f29100e0e0e0f0e0e0e0e0e0f0e0e0e0f29100e0e0e0f0e0e0e0e0e0f0e0e2a0f0e0f0e0e0e0f0e0e0e0e0e0f0e0e000d050000000000000000"

TV_POWER_ON = "260098017338100d0f2a0f0e0f0d0f0e0f0d0f0e0f0d0f0e0f0e0e0e0f0d0f0e0f29100e0e0e0f0d0f0d100e0e0e0f0e0e0e0f0d0f2a0f0e0f0d0f0d100d0f0e0f0d0f0d100d0f2a0f0e0f29102910291029100e0e0e0f29100d0f2a0f2b0e2a0f2a0f0e0f291000097f74380f0d1029100d0f0d100d0f0d100d0f0d100d0f0d100d0f0d100d0f2a0f0d100d0f0d100d0f0d100d0f0d100d0f0d1029100d0f0d100d0f0d100d0f0d100d0f0d1029100d0f2a0f2a0f2a0f2a0f0d100d0f2a0f0d1029102910291029100d0f2a0f00097e7338100e0e2a0f0e0f0e0e0e0f0e0e0e0f0e0e0e0f0d0f0e0f0e0e0e0f29100e0e0e0f0d0f0e0f0e0e0d100e0e0e0f0e0e2a0f0d100e0e0e0f0e0e0e0f0e0e0d100e0e2a0f0e0f291029102910290f0e0f0e0e2a0f0e0f2a0f2a0f2a0f2a0f0d0f2b0e00097f7338100d0f2a0f0d100e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e0d1029100e0e0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e2a0f0e0f0e0e0e0f0e0e0e0f0e0e0e0f0e0e2a0f0e0f291029102910290f0e0f0e0e2a0f0e0f29102a0f2a0f2a0f0e0e2a0f000d05"



STARHUB_MUTE = b'&\x00\x98\x00\x0f\x06\x08\x07\x08\x13\x06\x07\x08\x0e\x05\x14\x06\x0f\x04\n\x05\x08\x07\t\x05\x14\x06\x0f\x04\x14\x06\x08\x07\x07\x08\x18\x06\x0f\x05\x00\x0b\x8d\x0f\x07\x08\x08\x06\x14\x05\n\x04\x0f\x06\x13\x06\x0f\x05\n\x04\n\x05\n\x04\x15\x05\x0f\x05\x14\x06\x07\x08\x06\x08\x19\x06\x0f\x04\x00\x0b\x8d\x0f\x07\x08\x06\x08\x13\x06\x08\x07\x0f\x04\x14\x06\x0f\x05\n\x04\n\x05\t\x05\x15\x05\x0f\x05\x14\x06\x07\x08\x08\x05\x1a\x06\x0e\x06\x00\x0b\x8d\x0f\x06\x08\t\x05\x14\x06\x07\x08\x0e\x05\x14\x06\x0f\x04\n\x05\n\x04\n\x05\x15\x04\x10\x04\x14\x06\x08\x07\x07\x08\x18\x06\x0f\x05\x00\r\x05'

STARHUB_MUTE_TEST = "26006c000f0717120707080d0613070d0707080805140613070d071306080709041a060e06000b820f06070905150608050f0713060e060805230713060e061307070529070d07000b810f06070905150607070e0713060e0607070805150713060e061307060709051a070d07000d05000000000000000000000000"
STARHUB_POWER_ON = "26004c000f07080608130608070f0414060f0509050a05140613060e0614060708060819060708000b870f07080608130609050f0514060f0509050a05140613060e0614060905080719060905000d05000000000000000000000000"


# def start(bot, update):
#     """Send a message when the command /start is issued."""
#     update.message.reply_text('''
# Welcome to JoH IOT!

# List of possible commands:
# /ping - returns pong and current house selected
# /status - returns list of BL devices connected
# /keyboard <room> - returns keyboard access to devices and commands
# /list - returns the list of rooms and devices commands
# /on <device> - Turns on the targeted device
# /off <device> - Turns on the targeted device

# Future:
# /<device> temp <number
# /<device> volume <up/down>
# /<device> <option> <action>
# /listusers
# /adduser <user_name> <user_code>
# /removeuser <user_name> <user_code>
# /listtriggers
# /newtrigger
# /updatetrigger
# /deletetrigger
# /listhouse
# /switchhouse - prompts house?
# ''')


# def debug(bot, update):
#     update.message.reply_text("turning on/off")
#     print('at debug', DEVICES)
#     print(DEVICES[0].auth())
#     #DEVICES[0].send_data(DAIKIN_AIRCON_ON.encode())
#     DEVICES[0].send_data(bytearray.fromhex(''.join(STARHUB_MUTE_TEST)))


def error(bot, update, error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, error)


# def main():
#     TOKEN = '{}:{}'.format(BOT_ID, BOT_SECRET)
#     updater = Updater(TOKEN)

#     # Get the dispatcher to register handlers
#     dp = updater.dispatcher

#     dp.add_handler(CommandHandler("start", start))
#     dp.add_handler(CommandHandler("debug", debug))

#     # log all errors
#     dp.add_error_handler(error)

#     # Start the Bot
#     updater.start_polling()
#     print("running..")
#     print (DEVICES)
#     # Run the bot until you press Ctrl-C or the process receives SIGINT,
#     # SIGTERM or SIGABRT. This should be used most of the time, since
#     # start_polling() is non-blocking and will stop the bot gracefully.
#     updater.idle()


if __name__ == '__main__':
    #main()
    server.start_server()
