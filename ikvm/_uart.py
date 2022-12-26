# coding: utf-8
import struct
from functools import reduce
from operator import xor

# IMPORTANT: BAUDRATE must be same as baud of serial device
#  otherwise the hardware will reboot whenever server open the serial
#  hence controled host atx f-panel will get HIGH digital set
BAUDRATE = 19200
UART_TIMEOUT = 1 # second(s) timeout of serial write
UART_MAX_BUF = UART_TIMEOUT*BAUDRATE//10 # heuristic value prevent timeout, related to the board serial input buffer

## Arduino mouse button code
MOUSE_LEFT = 1
MOUSE_RIGHT = 2
MOUSE_MIDDLE = 4

""" protocol: magic command size content   checksum
              0F E0 --      --   -- -- ... --
command content                       comment
 10     [1B flag=00/01]+[1B key]      send click a single keyboard command, flag=01 press key, flag=00 release key
 11     [1B char]                     send enter a printable(and <Tab>/<LF>) character command
 12     n/a                           send release all pressed keys command
 20     [1B flag=00/01]+[1B button]   send click a mouse button command, flag=01 press button, flag=00 release button
 21     [1B x-move] [1B y-move]       send mouse move command, x- and y-move is signed char, i.e. between -128 and 127
 22     [1B orient=00/01]             send mouse scroll wheel command, orient=00 wheel down, orient=01 wheel up
 23     n/a                           send release all pressed mouse buttons command
 31     n/a                           send short power atx command
 32     n/a                           send reset atx command
 33     n/a                           send long power atx command
"""
MAGIC = b'\x0F\xE0'

CMD_KEY_CLICK   = 0x10
CMD_TEXT_ENTER  = 0x11
CMD_KEY_CLEAR   = 0x12
CMD_MOUSE_CLICK = 0x20
CMD_MOUSE_MOVE  = 0x21
CMD_MOUSE_WHEEL = 0x22
CMD_MOUSE_CLEAR = 0x23
CMD_SHORT_POWER = 0x31
CMD_RESET       = 0x32
CMD_LONG_POWER  = 0x33

checksum = lambda B: (reduce(xor, B)).to_bytes(1, 'big')

_key = lambda act, key: struct.pack('!2sBBBB', MAGIC, CMD_KEY_CLICK, 3, act, key)
_char = lambda char: struct.pack('!2sBBB', MAGIC, CMD_TEXT_ENTER, 2, char)
_kclr = struct.pack('!2sBB', MAGIC, CMD_KEY_CLEAR, 1)
UART_SEND_KEY = lambda act, key: _key(act, key)+checksum(_key(act, key))
UART_SEND_CHAR = lambda char: _char(char)+checksum(_char(char))
UART_SEND_KEY_CLEAR = _kclr + checksum(_kclr)

_mouse = lambda act, btn: struct.pack('!2sBBBB', MAGIC, CMD_MOUSE_CLICK, 3, act, btn)
_mv = lambda x, y:  struct.pack('!2sBBbb', MAGIC, CMD_MOUSE_MOVE, 3, x, y)
_scr = lambda flag: struct.pack('!2sBBB', MAGIC, CMD_MOUSE_WHEEL, 2, flag)
_mclr = struct.pack('!2sBB', MAGIC, CMD_MOUSE_CLEAR, 1)
UART_SEND_MOUSE_CLICK = lambda act, btn: _mouse(act, btn) + checksum(_mouse(act, btn))
UART_SEND_MOUSE_MOVE = lambda x, y: _mv(x, y) + checksum(_mv(x, y))
UART_SEND_MOUSE_WHEEL = lambda flag: _scr(flag)+checksum(_scr(flag))
UART_SEND_MOUSE_CLEAR = _mclr + checksum(_mclr)

_atx_convert = {0xFD: CMD_SHORT_POWER, 0xFE: CMD_RESET, 0xFF: CMD_LONG_POWER}
_atx = lambda sig: struct.pack('!2sBB', MAGIC, _atx_convert[sig], 1)
UART_SEND_ATX = lambda sig: _atx(sig) + checksum(_atx(sig))

__all__ = [
    'BAUDRATE',
    'UART_TIMEOUT',
    'UART_MAX_BUF',
    'MOUSE_LEFT',
    'MOUSE_RIGHT',
    'MOUSE_MIDDLE',
    'UART_SEND_KEY',
    'UART_SEND_CHAR',
    'UART_SEND_KEY_CLEAR',
    'UART_SEND_MOUSE_CLICK',
    'UART_SEND_MOUSE_MOVE',
    'UART_SEND_MOUSE_WHEEL',
    'UART_SEND_MOUSE_CLEAR',
    'UART_SEND_ATX',
]
