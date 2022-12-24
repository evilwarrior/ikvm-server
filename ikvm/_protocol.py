# coding: utf-8
"""
protocol:  magic    type content
           FF 31 D5 --   -- -- -- ...
type  content                              comment
 00   n/a                                  list all available uarts with vid:pid
                                            e.g. [('/dev/ttyUSB0', 0x0483, 0xdf11),]
 01   n/a                                  list all available uvc with resolution and frame rate
                                            e.g. [('/dev/video0', [((1920, 1080), [30, 15,]), ((1280, 960), [30, 15,]),]),]
 10                                        start/restart mjpg-streamer
      [1B {len}]+[{len}B cap]+              - video capture name (e.g. /dev/video0)
      [2B width]+[2B hight]+                - resolution (e.g. 07 80 04 38 meaning 1920x1080)
      [1B fps]+[2B port]                    - frame rate and server port
 20   [1B {len}]+[{len}B dev]              open specific uart
 21                                        send keyboard command
      1. [1B flag=80]+[2B {len}]+           - enter a character, char is printable or <Tab>/<LF> ascii characters
         [{len}B char]
      2. [1B flag=00/01]+[1B key]           - press/release a keyboard key, flag=01 press key, flag=00 release key
      3. [1B flag=02]                       - release all pressed keys
 22                                        send mouse command
      1. [1B flag=80]+[1B x-move]+          - move the mouse cursor
         [1B y-move]
      2. [1B flag=00/01]+[1B button]        - press/release a mouse button, flag=01 press button, flag=00 release key
      3. [1B flag=10/11]                    - mouse scroll wheel, flag=10 scroll down, floag=11 scroll up
      4. [1B flag=02]                       - release all mouse pressed buttons

 23   [1B sig]                             send atx command
                                            - 1. FD: Short Power
                                            - 2. FE: Reset
                                            - 3. FF: Long Power
 80                                        response of message type 00
      [1B num]+                             - number of available uart devices
      [1B {len}]+[{len}B dev]+              - uart device name
      [2B vid]+[2B pid]+...                 - uart device vid:pid
 81                                        response of message type 01
      [1B num]+...                          - number of available video captures
      [1B {len}]+[{len}B cap]+              - no.x video capture name (e.g. /dev/video0)
      [1B resnum]+...                       - number of available resolution on no.x video capture
      [2B width]+[2B hight]+                - no.y resolution (e.g. 07 80 04 38 meaning 1920x1080)
      [1B fpsnum]                           - number of available frame rates in no.y resolution
      [1B fps]+...                          - no.z frame rate
9X/AX [1B code] [1B {len}]+[{len}B detail] response of message type 1X/2X
                                            - 0x00 success; 0x01 failure
                                            - length allowed be 0
 FF   n/a                                  handshake message
 EE   n/a                                  goodbye message
"""
import struct

MAGIC = b'\xFF\x31\xD5'

TYPE_HANDSHAKE      = 0xFF
TYPE_GOODBYE        = 0xEE
TYPE_LIST_UART_REQ  = 0x00
TYPE_LIST_CAP_REQ   = 0x01
TYPE_RUN_MJPG_REQ   = 0x10
TYPE_OPEN_UART_REQ  = 0x20
TYPE_SEND_KEY_REQ   = 0x21
TYPE_SEND_MOUSE_REQ = 0x22
TYPE_SEND_ATX_REQ   = 0x23
TYPE_LIST_UART_RES  = 0x80
TYPE_LIST_CAP_RES   = 0x81
TYPE_RUN_MJPG_RES   = 0x90
TYPE_OPEN_UART_RES  = 0xA0
TYPE_SEND_KEY_RES   = 0xA1
TYPE_SEND_MOUSE_RES = 0xA2
TYPE_SEND_ATX_RES   = 0xA3

KEY_RELEASE   = 0x00
KEY_PRESS     = 0x01
KEY_CLEAR     = 0x02
KEY_TEXT_SEND = 0x80

MOUSE_RELEASE    = 0x00
MOUSE_PRESS      = 0x01
MOUSE_CLEAR      = 0x02
MOUSE_WHEEL_UP   = 0x10
MOUSE_WHEEL_DOWN = 0x11
MOUSE_MOVE       = 0x80

STATUS_SUCCESS = 0x00
STATUS_FAILURE = 0x01

ATX_SIGNAL  = {'short power': 0xFD, 'reset': 0xFE, 'long power': 0xFF}
STATUS_CODE = {STATUS_SUCCESS: 'success', STATUS_FAILURE: 'failure'}

HANDSHAKE_MSG    = struct.pack('!3sB', MAGIC, TYPE_HANDSHAKE)
GOODBYE_MSG      = struct.pack('!3sB', MAGIC, TYPE_GOODBYE)
LIST_UART_REQ    = struct.pack('!3sB', MAGIC, TYPE_LIST_UART_REQ)
LIST_CAP_REQ     = struct.pack('!3sB', MAGIC, TYPE_LIST_CAP_REQ)
RUN_MJPG_REQ     = lambda cap, res, fps, port:(
    struct.pack(
        '!3sBB%dsHHBH' %len(cap), MAGIC, TYPE_RUN_MJPG_REQ,
        len(cap), cap.encode('utf-8'),
        res[0], res[1], fps, port))
OPEN_UART_REQ    = lambda port:(
            struct.pack('!3sBB%ds' %len(port), MAGIC, TYPE_OPEN_UART_REQ, len(port), port.encode('utf-8'))
        )
SEND_KEY_REQ_K   = lambda act, key: struct.pack('!3sBBB', MAGIC, TYPE_SEND_KEY_REQ, act, key)
SEND_KEY_REQ_C   = lambda txt:(
        struct.pack('!3sBBH'+'B'*len(txt), MAGIC, TYPE_SEND_KEY_REQ, KEY_TEXT_SEND, len(txt), *[ord(char) for char in txt]))
SEND_KEY_REQ_R   = struct.pack('!3sBB', MAGIC, TYPE_SEND_KEY_REQ, KEY_CLEAR)
SEND_MOUSE_REQ_K = lambda act, btn: struct.pack('!3sBBB', MAGIC, TYPE_SEND_MOUSE_REQ, act, btn)
SEND_MOUSE_REQ_M = lambda x, y: struct.pack('!3sBBbb', MAGIC, TYPE_SEND_MOUSE_REQ, MOUSE_MOVE, x, y)
SEND_MOUSE_REQ_S = lambda flag: struct.pack('!3sBB', MAGIC, TYPE_SEND_MOUSE_REQ, flag) # both wheel and release all buttons
SEND_ATX_REQ     = lambda sig: struct.pack('!3sBB', MAGIC, TYPE_SEND_ATX_REQ, sig)
LIST_UART_RES    = lambda devs:( # e.g. devs = [('/dev/ttyUSB0', 0x0483, 0xdf11), ('/dev/ttyUSB1', 0x0483, 0xdf11),]
        struct.pack('!3sBB', MAGIC, TYPE_LIST_UART_RES, len(devs)) +
        b''.join([
            struct.pack(
                '!B%dsHH' %len(dev[0]), len(dev[0]), dev[0].encode('utf-8'),
                dev[1], dev[2]
            ) for dev in devs]
        ))
LIST_CAP_RES     = lambda devs:( # e.g. devs = [('/dev/video0', [((1920, 1080), [30, 15,]), ((1280, 960), [30, 15,]),]),]
        struct.pack('!3sBB', MAGIC, TYPE_LIST_CAP_RES, len(devs)) +
        b''.join([
            struct.pack('!B%dsB' %len(dev[0]), len(dev[0]), dev[0].encode('utf-8'), len(dev[1])) + 
            b''.join([
                struct.pack('!HHB', attr[0][0], attr[0][1], len(attr[1])) +
                b''.join([struct.pack('!B', fps) for fps in attr[1]]) for attr in dev[1]]
            ) for dev in devs]
        ))
STATUS_CODE_RES  = lambda TYPE, code, detail:(
        struct.pack(
            '!3sBBB%ds' %len(detail), MAGIC, TYPE,
            code, len(detail), detail.encode('utf-8')))

__all__ = [
        'MAGIC',
        'TYPE_HANDSHAKE',
        'TYPE_GOODBYE',
        'TYPE_LIST_UART_REQ',
        'TYPE_LIST_CAP_REQ',
        'TYPE_RUN_MJPG_REQ',
        'TYPE_OPEN_UART_REQ',
        'TYPE_SEND_KEY_REQ',
        'TYPE_SEND_MOUSE_REQ',
        'TYPE_SEND_ATX_REQ',
        'TYPE_LIST_UART_RES',
        'TYPE_LIST_CAP_RES',
        'TYPE_RUN_MJPG_RES',
        'TYPE_OPEN_UART_RES',
        'TYPE_SEND_KEY_RES',
        'TYPE_SEND_MOUSE_RES',
        'TYPE_SEND_ATX_RES',
        'KEY_RELEASE',
        'KEY_PRESS',
        'KEY_CLEAR',
        'KEY_TEXT_SEND',
        'MOUSE_RELEASE',
        'MOUSE_PRESS',
        'MOUSE_CLEAR',
        'MOUSE_WHEEL_UP',
        'MOUSE_WHEEL_DOWN',
        'MOUSE_MOVE',
        'STATUS_SUCCESS',
        'STATUS_FAILURE',
        'ATX_SIGNAL',
        'STATUS_CODE',
        'HANDSHAKE_MSG',
        'GOODBYE_MSG',
        'LIST_UART_REQ',
        'LIST_CAP_REQ',
        'RUN_MJPG_REQ',
        'OPEN_UART_REQ',
        'SEND_KEY_REQ_K',
        'SEND_KEY_REQ_C',
        'SEND_KEY_REQ_R',
        'SEND_MOUSE_REQ_K',
        'SEND_MOUSE_REQ_M',
        'SEND_MOUSE_REQ_S',
        'SEND_ATX_REQ',
        'LIST_UART_RES',
        'LIST_CAP_RES',
        'STATUS_CODE_RES',
]
