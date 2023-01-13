# coding: utf-8
if __name__ != 'ikvm.kvm':
    exit()
import socket, select, struct, serial, signal, subprocess, sys
import threading, asyncio, errno, os
import serial.tools.list_ports as list_ports
from sys import stdout, stderr
from copy import deepcopy as copy
from time import sleep, time
from functools import partial
from datetime import datetime
from base64 import b64encode
from ._globals import *
from ._protocol import *
from ._uart import *

get_start_mjpg_cmd = lambda root, cap_name, width, height, fps, mjpg_port: ' '.join((
    os.path.join(root, 'mjpg_streamer'),
    f'-i "input_uvc.so -d {cap_name} -r {width}x{height} -f {fps} -n"',
    f'-o "output_http.so -p {mjpg_port} -n"'
))

shell = partial(subprocess.run, capture_output=True, text=True)
raw = lambda string: repr(string).replace('\\\\', '\\')
base64 = lambda byte: b64encode(byte).decode()

def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    else:
        return True

class TermSigHandler:
    def __init__(self):
        self.run = True
        self.term = 0
        signal.signal(signal.SIGINT, self.__exit)
        signal.signal(signal.SIGTERM, self.__exit)

    def __exit(self, sig, frame):
        self.run = False
        self.term = sig

class Kvm:
    def __init__(self, port, bind='0.0.0.0', mjpg_root='', logfile=None, log_level=3, mjpg_logfile=None):
        self.port = port
        self.bind = bind
        self.logfile = logfile
        self.log_level = log_level
        self.mjpg_root = mjpg_root
        self.mjpg_logfile = mjpg_logfile
        self.__sock = None
        self.__accept = False # set to True when handshake success
        self.__uart = None
        self.__mjpg = None
        self.__mjpg_log = None
        self.__mjpg_cap_name = None
        self.__mjpg_resolution = None
        self.__mjpg_fps = None
        self.__mjpg_port = None
        self.__loop = None
        self.__alive_answer = False

    def start(self):
        # Open logfile
        self.__log_fh = open(self.logfile, 'a', LOG_BUFSIZE) if self.logfile else stdout
        self.__log_lock = threading.Lock() # used when thread write text into LOG
        # Socket settings
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) # disable Nagle Delay
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0) # enable ipv4/ipv6 dual-stack
        try:
            server.bind((self.bind, self.port))
        except socket.error as e:
            sys.exit(48)
        server.listen()
        server.setblocking(False)
        server.settimeout(SOCK_TIMEOUT)
        self.__log_write(4, 'Server socket is now listening')
        # Select settings
        self.__sockets = [server,] # only two members: ikvm server and client
        self.__w_queue = [] # whenever server want send message to client, the queue will be put client
        # Thread and Asynchronous settings
        self.__sockets_lock = threading.Lock() # used when thread modify self.__sockets and self.__sock
        self.__send_msg = b'' # put message in __send_async wait for sending
        self.__send_msg_lock = threading.Lock() # used when thread put msg in self.__send_msg
        # Start an event loop in a subthread for main thread put an I/O bound task
        self.__loop = asyncio.new_event_loop()
        threading.Thread(target=self.__loop.run_forever).start()
        self.__log_write(4, 'Event loop subthread started')
        # Setup terminal signal handler
        will = TermSigHandler()

        ip = self.bind[7:] if '.' in self.bind else self.bind # ip addr representation convert
        self.__log_write(3, f'Server bind with address {ip} started on port {self.port}')

        self.__buf = b''
        while will.run: # exit when server received SIGINT or SIGTERM signal
            with self.__sockets_lock:
                if len(self.__sockets) > 1 and self.__sockets[1].fileno() == -1:
                    # Clear invalid socket
                    with self.__send_msg_lock: # clear anything related with writting
                        self.__w_queue = []
                        self.__send_msg = b''
                    self.__sockets.pop()
                    self.__sock = None
                    self.__log_write(4, 'Clear the invalid socket in read and write queue')
                    continue
            r_sockets, w_sockets, _ = select.select(self.__sockets, self.__w_queue, [], SELECT_TIMEOUT)
            for sock in w_sockets:
                if sock is not self.__sock:
                    self.__w_queue.remove(sock)
                    self.__log_write(4, 'Got an unkown writer socket, removed')
                    continue
                elif sock.fileno() == -1:
                    self.__w_queue.remove(sock)
                    self.__log_write(4, 'Got an invalid writer socket, removed')
                    continue
                with self.__send_msg_lock:
                    msg = self.__send_msg
                    self.__send_msg = b'' # Clear __send_msg whatever
                    self.__w_queue = [] # Clear __w_queue
                if msg == b'': # Skip as no message for sending
                    break
                self.__send(msg)
                self.__log_write(4, 'Sent a message %s to client' %base64(msg)) # may not secure
                break

            for sock in r_sockets:
                ## Handle an incoming connection
                if sock is server:
                    self.__handle_incoming_connection(sock)
                    continue

                ## Handle client requests
                while sock is self.__sock:
                    ret = self.__recv_handler()
                    if ret == True:
                        continue
                    else:
                        break

        self.__log_write(3, 'Server terminated by %s' %('user' if will.term == signal.SIGINT else 'system',))
        ## Stop the event loop created previously
        self.__loop.call_soon_threadsafe(self.__loop.stop)
        self.__log_write(4, 'Event loop thread stopped')
        ## Say goodbye to existed client
        self.__say_goodbye()
        # close opened serial device
        if self.__uart and self.__uart.is_open:
            self.__uart.close()
            self.__log_write(3, 'Closed opened serial device')
        # close opened mjpg logfile
        if self.__mjpg_log and self.__mjpg_log is not stdout:
            self.__mjpg_log.close()
            self.__log_write(3, 'Closed opened mjpg logfile')
        # terminate running mjpg-streamer
        if self.__mjpg and process_alive(self.__mjpg.pid):
            os.killpg(os.getpgid(self.__mjpg.pid), signal.SIGINT)
            self.__log_write(3, 'Sent SIGINT to mjpg-streamer service')
            should_kill, timer = False, time()
            while process_alive(self.__mjpg.pid): # Wait until mjpg-streamer quit completely
                sleep(0.1)
                if time()-timer > WAIT_STOP_MJPG:
                    should_kill = True
                    break
            if should_kill:
                self.__log_write(2, 'Termination of mjpg-streamer service timeout')
                os.killpg(os.getpgid(self.__mjpg.pid), signal.SIGKILL)
                self.__log_write(2, 'Sent SIGKILL to mjpg-streamer service')
            self.__log_write(3, 'MJPG-Streamer service has been terminated')
        # Close socket
        server.close()
        self.__log_write(3, 'Server terminated completely')
        # close logfile
        if self.__log_fh and self.__log_fh is not stdout:
            self.__log_fh.close()

    def __log_write(self, level: int, txt):
        if self.log_level < level:
            return
        out = '{} [{}]: {}\n'.format(datetime.now().isoformat(), LOG_LEVEL[level], txt)
        with self.__log_lock:
            if self.__log_fh is stdout and level < 2:
                stderr.write(out)
            else:
                self.__log_fh.write(out)

    def __recv_handler(self): # True: continue, False: break
        if self.__buf == b'':
            res = self.__recv()
            if res is None: # non-block null return
                return True
            elif res is Quit:
                return False
            self.__buf += res
        if not self.__accept:
            if len(self.__buf) < 4: # continue as received message length not enough
                return True
            if self.__buf[:4] != HANDSHAKE_MSG:
                # Reject as incoming connection initially send invalid handshake message
                with self.__sockets_lock:
                    if self.__sock:
                        self.__sock.close()
                return False
        loc = self.__buf.find(MAGIC)
        if loc == -1:
            self.__buf = b''
            return True
        head = self.__buf[loc:][:4] # protocol magic and type
        if len(head) < 4:
            return True
        # Handle request individually (see __RECV_HANDLE_SWITCH for a specific function)
        case = Kvm.__RECV_HANDLE_SWITCH.get(head[-1])
        self.__buf = self.__buf[loc+4:] # trim magic and type
        case(self) if case else None
        return False

    def __list_available_caps(self):
        caps = shell('ls /dev/video*', shell=True)
        if caps.stderr:
            return []
        caps = caps.stdout.split()
        for cap in copy(caps):
            info = shell(('v4l2-ctl', '--info', '-d', cap))
            info = shell(('grep', '-A1', 'Device Caps'), input=info.stdout)
            info = shell(('sed', '-n', '2p'), input=info.stdout)
            info = shell(('xargs'), input=info.stdout)
            if info.stderr or info.stdout != 'Video Capture\n':
                caps.remove(cap) # Remove unavailable captures
        return caps

    async def __sleep_ask_alive(self):
        while self.__accept and not self.__alive_answer:
            await asyncio.sleep(0.01)

    async def __wait_ask_alive(self, client, ipport):
        try:
            await asyncio.wait_for(self.__sleep_ask_alive(), timeout=ASK_ALIVE_TIMEOUT)
        except asyncio.exceptions.TimeoutError:
            pass

        if self.__alive_answer:
            # reject if previous connection is alive
            client.close()
            self.__log_write(3, 'Received another connection from %s, rejected' %ipport)
        else:
            if self.__accept:
                # disconnect since receive ask alive response from old client timeout
                self.__disconnect('wait ask alive response timeout')
            # accept a connection from new client
            with self.__sockets_lock:
                self.__sockets.pop()
                self.__sockets.append(client)
                self.__sock = client
            self.__log_write(3, 'Received a connection from %s, accepted' %ipport)

    def __handle_incoming_connection(self, sock):
        client, ipport = sock.accept()
        client.settimeout(SOCK_TIMEOUT)
        ip = ipport[0][7:] if '.' in ipport[0] else f'[{ipport[0]}]' # ip addr representation convert
        ipport = f'{ip}:{ipport[1]}'

        with self.__sockets_lock:
            if len(self.__sockets) == 1:
                # accept a connection from client
                self.__sockets.append(client)
                self.__sock = client
                self.__log_write(3, 'Received a connection from %s, accepted' %ipport)
                return

        if not self.__accept:
            with self.__sockets_lock:
                # close old connection if the old client is not accepted
                self.__sock.close()
                # accept a connnection from new
                self.__sockets.pop()
                self.__sockets.append(client)
                self.__sock = client
            self.__log_write(3, 'Received a connection from %s, accepted' %ipport)
            return

        # send ask alive message to client
        self.__log_write(4, 'Sent ask alive message to client')
        self.__alive_answer = False
        self.__send_async(ASK_ALIVE_MSG)
        self.__log_write(5, 'Put the coroutine __wait_ask_alive into the event loop')
        self.__async_run(self.__wait_ask_alive(client, ipport))

    ## non-blocking socket.recv handling process
    def __recv(self):
        try:
            with self.__sockets_lock:
                if self.__sock:
                    recv = self.__sock.recv(BUF)
            if recv == b'':
                # Disconnected from client sent by FIN
                self.__disconnect('server got FIN')
                return Quit
            return recv
        except socket.error as e:
            if e.args[0] in (errno.EAGAIN, errno.EWOULDBLOCK,):
                # No data yet
                sleep(0.01)
                return None
            elif e.args[0] == errno.ECONNRESET:
                # Disconnected from client sent by RST
                self.__disconnect('server got RST')
                return Quit
            elif e.args[0] == errno.ECONNABORTED:
                # Disconnected since server aborted
                self.__disconnect('connection aborted')
                return Quit
            else:
                raise e
        except TimeoutError:
            # Disconnected since socket timed out
            self.__disconnect('socket timeout')
            return Quit

    ## secure socket.send
    def __send(self, msg):
        sent, timer = 0, 0
        while True:
            sleep(0.01)
            if timer > TIMEOUT_RT:
                return
            try:
                with self.__sockets_lock:
                    if self.__sock:
                        sent = self.__sock.send(msg)
            except socket.error as e:
                if e.args[0] == errno.ECONNRESET:
                    # Disconnected from client sent by RST
                    self.__disconnect('server got RST')
                    return
                elif e.args[0] == errno.ECONNABORTED:
                    # Disconnected since server aborted
                    self.__disconnect('connection aborted')
                    return
                else:
                    raise e
            except TimeoutError:
                # Disconnected since socket timed out
                self.__disconnect('socket timeout')
                return
            msg = msg[sent:]
            if len(msg) > 0:
                timer += 1
                continue
            return

    def __send_async(self, msg): # use function __send when in start() called select()
        with self.__send_msg_lock:
            self.__send_msg += msg
            with self.__sockets_lock:
                if self.__sock:
                    self.__w_queue.append(self.__sock)

    def __async_run(self, task):
        if not self.__loop:
            return
        self.__loop.call_soon_threadsafe(asyncio.create_task, task)

    def __handle_handshake(self):
        self.__log_write(3, 'Got a handshake message')
        self.__accept = True
        self.__log_write(5, 'Put handshake response to write queue')
        self.__send_async(HANDSHAKE_MSG)

    def __close_client(self, reason):
        self.__log_write(3, reason)
        if self.__uart and self.__uart.is_open:
            self.__uart.close()
            self.__log_write(3, 'Close the opened serial device')
        with self.__sockets_lock:
            if self.__sock:
                self.__sock.close()
        self.__accept = False
        self.__log_write(3, 'Closed the accepted client socket')

    def __disconnect(self, reason):
        self.__close_client('Disconnected the TCP as ' + reason)

    def __handle_goodbye(self):
        self.__close_client('Got a goodbye message')

    def __handle_reply_alive(self):
        self.__log_write(4, 'Got a replay alive message')
        self.__alive_answer = True

    def __handle_list_uarts_request(self):
        self.__log_write(4, 'Got a list uarts request message')
        devs = [(
            port.device,
            0 if port.vid is None else port.vid,
            0 if port.pid is None else port.pid,
        ) for port in list_ports.comports()]
        self.__log_write(5, 'Put a list uarts response to write queue')
        self.__send_async(LIST_UART_RES(devs))

    def __handle_list_captures_request(self):
        self.__log_write(4, 'Got a list captures request message')
        ## Get all available video captures
        caps = self.__list_available_caps()

        ## Get all resolution and frame rate
        devs = []
        for cap in caps:
            exts = shell(('v4l2-ctl', '--list-formats-ext', '-d', cap))
            exts = shell(('sed', '-n', '/MJPG/,/YUYV/p'), input=exts.stdout)
            exts = shell(('sed', '/MJPG/d'), input=exts.stdout)
            exts = shell(('sed', '/YUYV/d'), input=exts.stdout)
            exts = shell(('sed', '-r', 's/Size: Discrete//'), input=exts.stdout)
            exts = shell(r'sed -r "s/Interval: Discrete .*s \((.*) fps\)/\1/"', input=exts.stdout, shell=True)
            if exts.stderr:
                ## Send when shell command execution failed
                self.__log_write(1, 'Execution with video capture %s specs failure' %cap)
                self.__log_write(5, 'Put an empty list captures response to write queue')
                self.__send_async(LIST_CAP_RES([]))
                return
            exts = exts.stdout.split()
            attr, fps, pre_res = [], [], exts[0]
            for ext in exts:
                if 'x' in ext:
                    if ext == pre_res:
                        continue
                    attr.append((tuple(map(int, pre_res.split('x'))), copy(fps)))
                    pre_res = ext
                    fps = []
                else:
                    fps.append(int(float(ext)))
            devs.append((cap, attr))

        ## Send all available video captures with resolution and frame rate
        self.__log_write(5, 'Put a%s list captures response to write queue' %('' if devs else 'n empty',))
        self.__send_async(LIST_CAP_RES(devs))

    async def __start_mjpg_streamer(self, cap_name, width, height, fps, mjpg_port):
        # Check if restart mjpg-streamer
        if self.__mjpg and process_alive(self.__mjpg.pid):
            if( cap_name        == self.__mjpg_cap_name and
                (width, height) == self.__mjpg_resolution and
                fps             == self.__mjpg_fps and
                mjpg_port       == self.__mjpg_port
            ):
                # Reply if mjpg-streamer is running and with same parameters
                self.__log_write(4, 'MJPG-Streamer service already started')
                self.__log_write(5, 'Put a success run mjpg-streamer response to write queue')
                self.__send_async(STATUS_CODE_RES(TYPE_RUN_MJPG_RES, STATUS_SUCCESS, 'Already started'))
                return
            # Terminates subprocess when the settings from client was changed
            os.killpg(os.getpgid(self.__mjpg.pid), signal.SIGINT)
            self.__log_write(4, 'Sent SIGINT to mjpg-streamer service for the change of capture/specs')
            try:
                await asyncio.wait_for(self.__mjpg.wait(), timeout=WAIT_STOP_MJPG)
            except asyncio.exceptions.TimeoutError:
                # mjpg-streamer doesn't quit during WAIT_STOP_MJPG second(s)
                self.__log_write(2, 'Termination of mjpg-streamer service timeout')
                # Force terminates mjpg-streamer
                os.killpg(os.getpgid(self.__mjpg.pid), signal.SIGKILL)
                self.__log_write(2, 'Sent SIGKILL to mjpg-streamer service')
            self.__log_write(4, 'MJPG-Streamer service has been terminated')


        ## Set arguments of mjpg-streamer
        self.__mjpg_cap_name = cap_name
        self.__mjpg_resolution = (width, height)
        self.__mjpg_fps = fps
        self.__mjpg_port = mjpg_port
        cmd = get_start_mjpg_cmd(self.mjpg_root, cap_name, width, height, fps, mjpg_port)

        # Open logfile/stdout
        if not self.__mjpg_log:
            self.__mjpg_log = open(self.mjpg_logfile, 'w', LOG_BUFSIZE) if self.mjpg_logfile else stdout
        # Start MJPG-Streamer
        self.__mjpg = await asyncio.create_subprocess_shell(
                cmd, shell=True,
                stdout=self.__mjpg_log, stderr=self.__mjpg_log, # set mjpg-streamer logfile
                env=dict(os.environ, LD_LIBRARY_PATH=self.mjpg_root), # add enviroment variable
                preexec_fn=os.setsid) # add to process group for termination
        try:
            exit_code = await asyncio.wait_for(self.__mjpg.wait(), timeout=WAIT_START_MJPG)
        except asyncio.exceptions.TimeoutError:
            # Reply success if mjpg-streamer survive at least WAIT_START_MJPG second(s)
            self.__log_write(3, 'MJPG-Streamer service started with PID %d' %(self.__mjpg.pid,))
            self.__log_write(5, 'Put a success run mjpg-streamer response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_RUN_MJPG_RES, STATUS_SUCCESS, 'Started'))
        else:
            # Reply failure if mjpg-streamer exited
            self.__log_write(1, 'MJPG-Streamer service exited with status %d unexpected' %exit_code)
            self.__log_write(5, 'Put a failure run mjpg-streamer response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_RUN_MJPG_RES, STATUS_FAILURE,
                'Server Error: mjpg-streamer exited with status %d unexpected' %exit_code))

    def __handle_run_mjpg_request(self):
        self.__log_write(4, 'Got a run mjpg-streamer request message')
        ## Read video capture name
        cap_name = ''
        while True:
            if len(self.__buf) == 0: # for get name length
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            cap_name_len = self.__buf[0]
            if cap_name_len == 0:
                ## Reply if no video capture name found
                self.__log_write(2, 'Got the run mjpg-streamer request name length is 0')
                self.__log_write(5, 'Put a failure run mjpg-streamer response to write queue')
                self.__send_async(STATUS_CODE_RES(
                    TYPE_RUN_MJPG_RES, STATUS_FAILURE,
                    'Protocol Error: Video capture name length is 0'))
                return
            elif len(self.__buf)-1 < cap_name_len: # for get name
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            try:
                cap_name = self.__buf[1:][:cap_name_len].decode('utf-8')
            except UnicodeDecodeError:
                ## Reply if serial device name is not UTF-8 encoding
                self.__log_write(2, 'Resolved the run mjpg-streamer request video capture name failed')
                self.__log_write(5, 'Put a failure run mjpg-streamer response to write queue')
                self.__send_async(STATUS_CODE_RES(
                    TYPE_RUN_MJPG_RES, STATUS_FAILURE,
                    'Protocol Error: Video capture name is not valid UTF-8 encoding'))
                return
            self.__buf = self.__buf[1+cap_name_len:]
            break

        ## Read resolution
        width, height = 0, 0
        while True:
            if len(self.__buf) < 4:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            width, height = struct.unpack('!HH', self.__buf[:4])
            self.__buf = self.__buf[4:]
            break

        ## Read frame rate
        fps = 0
        while True:
            if len(self.__buf) == 0: # for get frame rate
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            fps = self.__buf[0]
            self.__buf = self.__buf[1:]
            break

        ## Read required mjpg-streamer port
        mjpg_port = 0
        while True:
            if len(self.__buf) < 2:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            mjpg_port, = struct.unpack('!H', self.__buf[:2])
            self.__buf = self.__buf[2:]
            break

        ## Search partial matched video capture
        match = list(filter(lambda cap: cap_name in cap, self.__list_available_caps()))
        if not match:
            self.__log_write(3, 'Client requests to start up an unavailable video capture')
            self.__log_write(5, 'Put a failure run mjpg-streamer response to write queue')
            secure_name = cap_name[:216]
            self.__send_async(STATUS_CODE_RES(
                TYPE_RUN_MJPG_RES, STATUS_FAILURE,
                'Server Error: No such video capture "%s"' %secure_name))
            return
        cap_name = match[0]

        ## Async run mjpg-streamer
        self.__log_write(5, 'Put the coroutine __start_mjpg_streamer into the event loop')
        self.__async_run(self.__start_mjpg_streamer(cap_name, width, height, fps, mjpg_port))

    def __handle_open_uart_request(self):
        self.__log_write(4, 'Got a open uart request message')
        ## Read serial device name
        uart_name = ''
        while True:
            if len(self.__buf) == 0: # for get name length
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            uart_name_len = self.__buf[0]
            if uart_name_len == 0:
                ## Reply if no serial device name found
                self.__log_write(2, 'Got the open uart request name length is 0')
                self.__log_write(5, 'Put a failure open uart response to write queue')
                self.__send_async(STATUS_CODE_RES(
                    TYPE_OPEN_UART_RES, STATUS_FAILURE,
                    'Protocol Error: Serial device name length is 0'))
                return
            elif len(self.__buf)-1 < uart_name_len: # for get name
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            try:
                uart_name = self.__buf[1:][:uart_name_len].decode('utf-8')
            except UnicodeDecodeError:
                ## Reply if serial device name is not UTF-8 encoding
                self.__log_write(2, 'Resolved the open uart request serial device name failed')
                self.__log_write(5, 'Put a failure open uart response to write queue')
                self.__send_async(STATUS_CODE_RES(
                    TYPE_OPEN_UART_RES, STATUS_FAILURE,
                    'Protocol Error: Serial device name is not valid UTF-8 encoding'))
                return
            self.__buf = self.__buf[1+uart_name_len:]
            break

        ## Search partial matched serial device on local
        for uart_port in list_ports.grep(uart_name):
            ## Open serial device
            try:
                if self.__uart is None:
                    msg = 'Opened'
                    self.__uart = serial.Serial(uart_port.device, BAUDRATE, write_timeout=UART_TIMEOUT)
                elif self.__uart.port == uart_port.device:
                    if self.__uart.is_open:
                        msg = 'Already opened'
                    else:
                        msg = 'Re-opened'
                        self.__uart.open()
                else:
                    secure_name = uart_port.device[:239]
                    msg = f'Changed from "{secure_name}"'
                    if self.__uart.is_open:
                        self.__uart.close()
                        self.__uart = serial.Serial(uart_port.device, BAUDRATE, write_timeout=UART_TIMEOUT)
            except serial.SerialException:
                self.__log_write(1, 'Open serial device %s failed' %uart_port.device)
                self.__log_write(5, 'Put a failure open uart response to write queue')
                secure_name = uart_port.device[:219]
                self.__send_async(STATUS_CODE_RES(
                    TYPE_OPEN_UART_RES, STATUS_FAILURE, 
                    f'Serial Error: Cannot open device "{secure_name}"'))
                return
            ## Reply success message
            self.__log_write(3, '%s serial device %s' %(msg+' to' if msg[0] == 'C' else msg, uart_port.device))
            self.__log_write(5, 'Put a success open uart response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_OPEN_UART_RES, STATUS_SUCCESS, msg))
            return

        ## Reply no devicees failure message
        self.__log_write(3, 'Client requests to open an unavailable serial device')
        self.__log_write(5, 'Put a failure open uart response to write queue')
        secure_name = uart_name[:223]
        self.__send_async(STATUS_CODE_RES(TYPE_OPEN_UART_RES, STATUS_FAILURE,
            f'Server Error: No such device "{secure_name}"'))

    def __send_key_to_uart(self, act, key):
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Send key failed as serial device not opened')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_KEY_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        press = 'press' if act == KEY_PRESS else 'release'
        # Determine detail be with printable key or hex code
        key_txt = '"%s"' %chr(key) if chr(key).isprintable() and key in range(0x80) else '<{:02X}>'.format(key)
        cmd = UART_SEND_KEY(act, key) # construct serial protocol format
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send key to serial device timeout
            self.__log_write(1, 'Send key command to serial timeout')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_KEY_RES, STATUS_FAILURE,
                f'Serial Error: Send {press} key {key_txt} timeout'))
        else:
            ## Send success message
            self.__log_write(4, 'Send key command to serial success')
            self.__log_write(5, 'Put a success send key response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_KEY_RES, STATUS_SUCCESS, f'Key {key_txt} {press}'))

    def __send_text_chars_to_uart(self, chars):
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Send text characters failed as serial device not opened')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_KEY_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        # Prepare shown characters in replay message
        chrs = raw(''.join([chr(char) for char in chars][:MAX_SHOW]))
        # divide a single command to multiple commands that can be handled with hardware
        cmds = b''.join([UART_SEND_CHAR(char) for char in chars])
        try:
            while len(cmds) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmds[:UART_MAX_BUF])
                cmds = cmds[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send key to serial device timeout
            self.__log_write(1, 'Send text characters command to serial timeout')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_KEY_RES, STATUS_FAILURE, f'Serial Error: Send text characters started with {chrs} timeout'))
        else:
            ## Send success message
            self.__log_write(4, 'Send text characters command to serial success')
            self.__log_write(5, 'Put a success send key response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_KEY_RES, STATUS_SUCCESS, f'Send text characters started with {chrs} success'))

    def __send_clear_keys_to_uart(self):
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Release all keys failed as serial device not opened')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_KEY_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        cmd = UART_SEND_KEY_CLEAR
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send key to serial device timeout
            self.__log_write(1, 'Send release all keys command to serial timeout')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_KEY_RES, STATUS_FAILURE, 'Serial Error: Send release all keys command timeout'))
        else:
            ## Send success message
            self.__log_write(4, 'Send release all keys command to serial success')
            self.__log_write(5, 'Put a success send key response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_KEY_RES, STATUS_SUCCESS, 'Send release all keys command success'))

    def __handle_send_key_request(self):
        self.__log_write(4, 'Got a send key request message')
        ## Read flag
        flag = 0
        while True:
            if len(self.__buf) == 0:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            flag = self.__buf[0]
            self.__buf = self.__buf[1:]
            break

        if flag not in (KEY_TEXT_SEND, KEY_PRESS, KEY_RELEASE, KEY_CLEAR):
            ## Send failure message when first byte of message is invalid
            self.__log_write(2, 'Got the send key request invalid flag <{:02X}>'.format(flag))
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_KEY_RES, STATUS_FAILURE,
                'Protocol Error: Received flag <{:02X}> is invalid'.format(flag)))
            return

        ## send release all keys command to controled host when flag is KEY_CLEAR
        if flag == KEY_CLEAR:
            self.__send_clear_keys_to_uart()
            return

        ## Read is_press and key when flag is in KEY_PRESS or KEY_RELEASE
        if flag in (KEY_PRESS, KEY_RELEASE):
            while True:
                key = 0
                if len(self.__buf) == 0:
                    res = self.__recv()
                    if res is None:
                        continue
                    elif res is Quit:
                        return
                    self.__buf += res
                    continue
                key = self.__buf[0]
                self.__buf = self.__buf[1:]
                break
            self.__send_key_to_uart(flag, key)
            return

        ## Read text characters when flag is KEY_TEXT_SEND
        # Read length of text
        txt_len = 0
        while True:
            if len(self.__buf) < 2:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            txt_len, = struct.unpack('!H', self.__buf[:2])
            self.__buf = self.__buf[2:]
            break

        if txt_len == 0:
            ## Send failure message when length of is_press and keys are zero
            self.__log_write(2, 'The flag KEY_TEXT_SEND followed zero commands length')
            self.__log_write(5, 'Put a failure send key response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_KEY_RES, STATUS_FAILURE,
                'Protocol Error: the flag KEY_TEXT_SEND followed zero commands length'))
            return

        # Read text characters
        chars = b''
        while True:
            if len(self.__buf) < txt_len:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            chars = self.__buf[:txt_len]
            self.__buf = self.__buf[txt_len:]
            break

        self.__send_text_chars_to_uart(chars)

    def __send_clear_mouse_buttons_to_uart(self):
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Release all mouse buttons failed as serial device not opened')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_MOUSE_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        cmd = UART_SEND_MOUSE_CLEAR
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send mouse to serial device timeout
            self.__log_write(1, 'Send release all mouse buttons command to serial timeout')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_FAILURE, 'Serial Error: Send release all mouse buttons command timeout'))
        else:
            ## Send success message
            self.__log_write(4, 'Send release all mouse buttons command to serial success')
            self.__log_write(5, 'Put a success send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_SUCCESS, 'Send release all mouse buttons command success'))

    def __send_mouse_scroll_wheel_to_uart(self, flag):
        orient = 'up' if flag == MOUSE_WHEEL_UP else 'down'
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, f'Send mouse scroll wheel {orient} command failed as serial device not opened')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_MOUSE_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        cmd = UART_SEND_MOUSE_WHEEL(flag&0x0F) # transform flag 0x10/0x11 to 0x00/0x01
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send mouse to serial device timeout
            self.__log_write(1, f'Send mouse scroll wheel {orient} command to serial timeout')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_FAILURE, f'Serial Error: Send mouse scroll wheel {orient} command timeout'))
        else:
            ## Send success message
            self.__log_write(4, f'Send mouse scroll wheel {orient} command to serial success')
            self.__log_write(5, 'Put a success send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_SUCCESS, f'Mouse scrolled wheel {orient}'))

    def __send_click_mouse_butten_to_uart(self, act, button):
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Send click mouse button failed as serial device not opened')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_MOUSE_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        if button not in (MOUSE_LEFT, MOUSE_RIGHT, MOUSE_MIDDLE):
            self.__log_write(2, 'Received invalid click mouse button code <{:02X}>'.format(button))
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_MOUSE_RES, STATUS_FAILURE,
                'Invalid mouse button <{:02X}>'.format(button)))
            return

        press = 'press' if act == MOUSE_PRESS else 'release'
        btn_txt = 'left' if button == MOUSE_LEFT else ('right' if button == MOUSE_RIGHT else 'middle')
        cmd = UART_SEND_MOUSE_CLICK(act, button) # construct serial protocol format
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send mouse to serial device timeout
            self.__log_write(1, 'Send click mouse button command to serial timeout')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_FAILURE, f'Serial Error: Send {press} mouse button {btn_txt} timeout'))
        else:
            ## Send success message
            self.__log_write(4, 'Send click mouse button command to serial success')
            self.__log_write(5, 'Put a success send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_MOUSE_RES, STATUS_SUCCESS, f'Mouse button {btn_txt} {press}'))

    def __send_mouse_move_to_uart(self, x, y):
        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Send mouse move command failed as serial device not opened')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_MOUSE_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        cmd = UART_SEND_MOUSE_MOVE(x, y) # construct serial protocol format
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send mouse to serial device timeout
            self.__log_write(1, 'Send mouse move command to serial timeout')
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_FAILURE, 'Serial Error: Send mouse move command timeout'))
        else:
            ## Send success message
            self.__log_write(4, 'Send mouse move command to serial success')
            self.__log_write(5, 'Put a success send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_SUCCESS, f'Mouse shifted ({x}, {y})'))

    def __handle_send_mouse_request(self):
        self.__log_write(4, 'Got a send mouse request message')
        ## Read flag
        flag = 0
        while True:
            if len(self.__buf) == 0:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            flag = self.__buf[0]
            self.__buf = self.__buf[1:]
            break

        if flag not in (MOUSE_RELEASE, MOUSE_PRESS, MOUSE_CLEAR, MOUSE_WHEEL_UP, MOUSE_WHEEL_DOWN, MOUSE_MOVE):
            ## Send failure message when first byte of message is invalid
            self.__log_write(2, 'Got the send mouse request invalid flag <{:02X}>'.format(flag))
            self.__log_write(5, 'Put a failure send mouse response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_MOUSE_RES, STATUS_FAILURE,
                'Protocol Error: Received flag <{:02X}> is invalid'.format(flag)))
            return

        ## send release all mouse buttons command to controled host when flag is MOUSE_CLEAR
        if flag == MOUSE_CLEAR:
            self.__send_clear_mouse_buttons_to_uart()
            return

        ## send mouse scroll wheel command to controled host when flag is MOUSE_WHEEL_XX
        if flag in (MOUSE_WHEEL_UP, MOUSE_WHEEL_DOWN):
            self.__send_mouse_scroll_wheel_to_uart(flag)
            return

        ## Read is_press and button when flag is in MOUSE_PRESS or MOUSE_RELEASE
        if flag in (MOUSE_PRESS, MOUSE_RELEASE):
            while True:
                btn = 0
                if len(self.__buf) == 0:
                    res = self.__recv()
                    if res is None:
                        continue
                    elif res is Quit:
                        return
                    self.__buf += res
                    continue
                btn = self.__buf[0]
                self.__buf = self.__buf[1:]
                break
            self.__send_click_mouse_butten_to_uart(flag, btn)
            return

        ## Read x-move and y-move when flag is MOUSE_MOVE
        x, y = 0, 0
        while True:
            if len(self.__buf) < 2:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            x, y = struct.unpack('!bb', self.__buf[:2])
            self.__buf = self.__buf[2:]
            break

        self.__send_mouse_move_to_uart(x, y)

    def __handle_send_atx_request(self):
        self.__log_write(4, 'Got a send atx request message')
        ## Read atx signal
        key = b''
        while True:
            if len(self.__buf) == 0:
                res = self.__recv()
                if res is None:
                    continue
                elif res is Quit:
                    return
                self.__buf += res
                continue
            sig = self.__buf[0]
            self.__buf = self.__buf[1:]
            break

        if sig not in ATX_SIGNAL.values():
            ## Send failure message when signal is invalid
            self.__log_write(2, 'Got the send atx request invalid signal <{:02X}>'.format(sig))
            self.__log_write(5, 'Put a failure send atx response to write queue')
            self.__send_async(STATUS_CODE_RES(
                TYPE_SEND_ATX_RES, STATUS_FAILURE,
                'Protocol Error: Received invalid signal <{:02X}>'.format(sig)))
            return

        if self.__uart is None or not self.__uart.is_open:
            ## Send failure message when serial device is not opened
            self.__log_write(1, 'Send atx signal failed as serial device not opened')
            self.__log_write(5, 'Put a failure send atx response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_ATX_RES, STATUS_FAILURE, 'Serial Error: Device not opened'))
            return

        cmd = UART_SEND_ATX(sig)
        try:
            while len(cmd) > 0: # send all bytes to serial device
                sent = self.__uart.write(cmd)
                cmd = cmd[sent:]
        except serial.SerialTimeoutException:
            ## Send failure message when send key to serial device timeout
            self.__log_write(1, 'Send atx signal to serial timeout')
            self.__log_write(5, 'Put a failure send atx response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_ATX_RES, STATUS_FAILURE,
                'Serial Error: Send signal <{:02X}> timeout'.format(sig)))
        else:
            ## Send success message
            self.__log_write(3, 'Send atx signal to serial success')
            self.__log_write(5, 'Put a success send atx response to write queue')
            self.__send_async(STATUS_CODE_RES(TYPE_SEND_ATX_RES, STATUS_SUCCESS,
                'Signal <{:02X}> sent'.format(sig)))

    __RECV_HANDLE_SWITCH = {
        TYPE_HANDSHAKE: __handle_handshake,
        TYPE_GOODBYE: __handle_goodbye,
        #TYPE_ASK_ALIVE: __handle_ask_alive,
        TYPE_REPLY_ALIVE: __handle_reply_alive,
        TYPE_LIST_UART_REQ: __handle_list_uarts_request,
        TYPE_LIST_CAP_REQ: __handle_list_captures_request,
        TYPE_RUN_MJPG_REQ: __handle_run_mjpg_request,
        TYPE_OPEN_UART_REQ: __handle_open_uart_request,
        TYPE_SEND_KEY_REQ: __handle_send_key_request,
        TYPE_SEND_MOUSE_REQ: __handle_send_mouse_request,
        TYPE_SEND_ATX_REQ: __handle_send_atx_request,}

    def __say_goodbye(self):
        if self.__sock:
            self.__send_async(GOODBYE_MSG)
            self.__log_write(3, 'Sent goodbye message to client')
        self.__accept = False
