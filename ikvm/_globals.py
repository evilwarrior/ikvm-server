# coding: utf-8
import re

ipv6_re = r'(?:^|(?<=\s))(([0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:)|fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|::(ffff(:0{1,4}){0,1}:){0,1}((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])|([0-9a-fA-F]{1,4}:){1,4}:((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9]))(?=\s|$)'

def address_family(ip):
    if re.match(ipv6_re, ip) is not None:
        return 'ipv6'
    try:
        ipv4 = tuple(map(int, ip.split('.')))
    except ValueError:
        raise TypeError(f'ip "{ip}" with invalid format')
    if len(ipv4) != 4 or any([byte not in range(0x100) for byte in ipv4]):
        raise TypeError(f'ip "{ip}" with invalid format')
    return 'ipv4'

LOG_LEVEL = ('FATAL', 'ERROR', 'WARN', 'INFO', 'DEBUG', 'TRACE')
LOG_BUFSIZE = 1
SELECT_TIMEOUT  = 1 # second(s)
WAIT_START_MJPG = 0.1 # second(s) wait mjpg-streamer killing
WAIT_STOP_MJPG  = 2.2 # second(s) wait mjpg-streamer killing
MAX_SHOW        = 20 # replay max batch send keys showed in detail

BUF = 1024
TIMEOUT_RT = 10  # used in socket send (real-time)
ASK_ALIVE_TIMEOUT = 2 # second(s) wait ask alive response
SOCK_TIMEOUT = 60 # second(s) socket timeout

class UserDefinedQuit:
    pass
Quit = UserDefinedQuit() # Used when peer disconnect unexpected

__all__ = [
        'address_family',
        'LOG_LEVEL',
        'LOG_BUFSIZE',
        'SELECT_TIMEOUT',
        'WAIT_START_MJPG',
        'WAIT_STOP_MJPG',
        'MAX_SHOW',
        'BUF',
        'TIMEOUT_RT',
        'ASK_ALIVE_TIMEOUT',
        'SOCK_TIMEOUT',
        'Quit',
]
