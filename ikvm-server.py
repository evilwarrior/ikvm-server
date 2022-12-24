#!/usr/bin/env python3
# coding: utf-8

import sys
from platform import system

if __name__ != '__main__' or system() != 'Linux':
    sys.exit(1)

import argparse, os, shutil
from ikvm._globals import address_family

def _port(port):
    if int(port) not in range(1, 0x10000):
        raise argparse.ArgumentTypeError('Port value out of range')
    return int(port)

def _bind(ip):
    if address_family(ip) == 'ipv4':
        ip = '::ffff:'+ip
    return ip

def _mjpg_root(root):
    if not shutil.which(os.path.join(root, 'mjpg_streamer')):
        root = 'path "{}"'.format(root) if root else 'system environment path'
        raise argparse.ArgumentTypeError('Executable file "mjpg_streamer" not found in {}'.format(root))
    return root

def _logfile(logfile):
    folder = os.path.dirname(logfile) if os.path.dirname(logfile) else './'
    if not os.path.isdir(folder):
        raise argparse.ArgumentTypeError('Path "{}" does not exist'.format(folder))
    if os.path.isdir(logfile):
        raise argparse.ArgumentTypeError('Log file "{}" should not be a folder'.format(logfile))
    return logfile

def _log_level(log_level):
    if int(log_level) not in range(6):
        raise argparse.ArgumentTypeError('Log level should be between 0 and 5')
    return int(log_level)

parser = argparse.ArgumentParser()
parser.add_argument('port', type=_port, default=7130, nargs='?', help='iKVM server port, default 7130')
parser.add_argument('-B', '--bind', type=_bind, default='::1', help='iKVM server bind listening address, default "::1"')
parser.add_argument('--mjpg-root', type=_mjpg_root, default='', help='MJPG-Streamer root path, default use system enviroment')
parser.add_argument('--logfile', type=_logfile, help='iKVM server saved log file path, default SYSOUT and SYSERR')
parser.add_argument('--log-level', type=_log_level, default=3, help='log level used, default 3')
parser.add_argument('--mjpg-logfile', type=_logfile, help='MJPG-Streamer service saved log file path, default SYSOUT')

# set input arguments
args = parser.parse_args()
port = args.port
mjpg_root = args.mjpg_root
logfile = args.logfile
log_level = args.log_level
mjpg_logfile = args.mjpg_logfile
bind = args.bind

from ikvm.kvm import Kvm
kvm = Kvm(port, bind, mjpg_root, logfile, log_level, mjpg_logfile)
kvm.start()
sys.exit(0)
