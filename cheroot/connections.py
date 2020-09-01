"""Utilities to manage open connections."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import collections
import io
import os
import socket
import threading
import time

from . import errors
from ._compat import selectors
from ._compat import socketpair
from ._compat import suppress
from .makefile import MakeFile

import six

try:
    import fcntl
except ImportError:
    try:
        from ctypes import windll, WinError
        import ctypes.wintypes
        _SetHandleInformation = windll.kernel32.SetHandleInformation
        _SetHandleInformation.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
        ]
        _SetHandleInformation.restype = ctypes.wintypes.BOOL
    except ImportError:
        def prevent_socket_inheritance(sock):
            """Stub inheritance prevention.

            Dummy function, since neither fcntl nor ctypes are available.
            """
            pass
    else:
        def prevent_socket_inheritance(sock):
            """Mark the given socket fd as non-inheritable (Windows)."""
            if not _SetHandleInformation(sock.fileno(), 1, 0):
                raise WinError()
else:
    def prevent_socket_inheritance(sock):
        """Mark the given socket fd as non-inheritable (POSIX)."""
        fd = sock.fileno()
        old_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, old_flags | fcntl.FD_CLOEXEC)


class ConnectionManager:
    """Class which manages HTTPConnection objects.

    This is for connections which are being kept-alive for follow-up requests.
    """

    # NOTE: The selector must only be accessed within the thread
    # running server.serve().

    _CTRL_MSG_PUT = b'P'

    def __init__(self, server):
        """Initialize ConnectionManager object.

        Args:
            server (cheroot.server.HTTPServer): web server object
                that uses this ConnectionManager instance.
        """
        self.server = server
        self._readable_conns = collections.deque()
        self._put_q = collections.deque()
        self._selector = selectors.DefaultSelector()

        # _num_conns tracks the number of HTTPConnection objects
        # being managed, which may be in one of:
        #
        # * _readable_conns
        # * the selector
        # * _put_q
        #
        # it is tracked independently of those containers to ensure a stable
        # count, even as conns are transferred between them
        self._num_conns = 0

        self._selector.register(
            server.socket.fileno(),
            selectors.EVENT_READ, data=server,
        )

        self._ctrl_rx, self._ctrl_tx = socketpair()
        self._selector.register(
            self._ctrl_rx.fileno(),
            selectors.EVENT_READ, data=self._ctrl_rx,
        )
        # protects writes to self._ctrl_tx
        self._ctrl_lock = threading.Lock()

    def _pop_readable_conn(self):
        conn = self._readable_conns.popleft()
        self._num_conns -= 1
        return conn

    def _remove_conn(self, sock_fd, conn):
        self._selector.unregister(sock_fd)
        conn.close()
        self._num_conns -= 1

    def put(self, conn):
        """Put idle connection into the ConnectionManager to be managed.

        Args:
            conn (cheroot.server.HTTPConnection): HTTP connection
                to be managed.
        """
        self._num_conns += 1
        conn.last_used = time.time()

        # if this conn has more data waiting to be read,
        # store it in the readable queue.
        if conn.rfile.has_data():
            self._readable_conns.append(conn)
            return

        # otherwise, register it with the selector.
        self._put_q.append(conn)
        with self._ctrl_lock:
            self._ctrl_tx.send(self._CTRL_MSG_PUT)

    def _get_selector_conns(self):
        """Retrieve client connections registered with the selector."""
        for _, (_, sock_fd, _, conn) in self._selector.get_map().items():
            if conn not in {self.server, self._ctrl_rx}:
                yield (sock_fd, conn)

    def expire(self):
        """Expire least recently used connections.

        This happens if there are either too many open connections, or if the
        connections have been timed out.

        This should be called periodically.
        """
        # find any connections still registered with the selector
        # that have not been active recently enough.
        threshold = time.time() - self.server.timeout
        timed_out_connections = [
            (sock_fd, conn)
            for (sock_fd, conn) in self._get_selector_conns()
            if conn.last_used < threshold
        ]
        for sock_fd, conn in timed_out_connections:
            self._remove_conn(sock_fd, conn)

    def get_conn(self):
        """Return a HTTPConnection object which is ready to be handled.

        A connection returned by this method should be ready for a worker
        to handle it. If there are no connections ready, None will be
        returned.

        Any connection returned by this method will need to be `put`
        back if it should be examined again for another request.

        Returns:
            cheroot.server.HTTPConnection instance, or None.

        """
        # return a readable connection if any exist
        with suppress(IndexError):
            return self._pop_readable_conn()

        # Will require a select call.
        try:
            # The timeout value impacts performance and should be carefully
            # chosen. Ref:
            # github.com/cherrypy/cheroot/issues/305#issuecomment-663985165
            rlist = [
                key for key, _
                in self._selector.select(timeout=0.01)
            ]
        except OSError:
            self._remove_invalid_sockets()
            # Wait for the next tick to occur.
            return None

        for key in rlist:
            if key.data is self._ctrl_rx:
                self._process_ctrl_msg()
                continue

            if key.data is self.server:
                # New connection
                return self._from_server_socket(self.server.socket)

            conn = key.data
            # unregister connection from the selector until the server
            # has read from it and returned it via put()
            self._selector.unregister(key.fd)
            self._readable_conns.append(conn)

        try:
            return self._pop_readable_conn()
        except IndexError:
            return None

    def _process_ctrl_msg(self):
        msg = self._ctrl_rx.recv(1)

        if msg == self._CTRL_MSG_PUT:
            conn = self._put_q.popleft()
            self._selector.register(
                conn.socket.fileno(), selectors.EVENT_READ, data=conn,
            )

    def _remove_invalid_sockets(self):
        # Mark any connection which no longer appears valid
        # If the server or ctrl sockets are invalid,
        # we'll just shutdown.
        invalid_conns = []
        for sock_fd, conn in self._get_selector_conns():
            try:
                os.fstat(sock_fd)
            except OSError:
                invalid_conns.append((sock_fd, conn))

        for sock_fd, conn in invalid_conns:
            self._remove_conn(sock_fd, conn)

    def _from_server_socket(self, server_socket):  # noqa: C901  # FIXME
        try:
            s, addr = server_socket.accept()
            if self.server.stats['Enabled']:
                self.server.stats['Accepts'] += 1
            prevent_socket_inheritance(s)
            if hasattr(s, 'settimeout'):
                s.settimeout(self.server.timeout)

            mf = MakeFile
            ssl_env = {}
            # if ssl cert and key are set, we try to be a secure HTTP server
            if self.server.ssl_adapter is not None:
                try:
                    s, ssl_env = self.server.ssl_adapter.wrap(s)
                except errors.NoSSLError:
                    msg = (
                        'The client sent a plain HTTP request, but '
                        'this server only speaks HTTPS on this port.'
                    )
                    buf = [
                        '%s 400 Bad Request\r\n' % self.server.protocol,
                        'Content-Length: %s\r\n' % len(msg),
                        'Content-Type: text/plain\r\n\r\n',
                        msg,
                    ]

                    sock_to_make = s if not six.PY2 else s._sock
                    wfile = mf(sock_to_make, 'wb', io.DEFAULT_BUFFER_SIZE)
                    try:
                        wfile.write(''.join(buf).encode('ISO-8859-1'))
                    except socket.error as ex:
                        if ex.args[0] not in errors.socket_errors_to_ignore:
                            raise
                    return
                if not s:
                    return
                mf = self.server.ssl_adapter.makefile
                # Re-apply our timeout since we may have a new socket object
                if hasattr(s, 'settimeout'):
                    s.settimeout(self.server.timeout)

            conn = self.server.ConnectionClass(self.server, s, mf)

            if not isinstance(
                    self.server.bind_addr,
                    (six.text_type, six.binary_type),
            ):
                # optional values
                # Until we do DNS lookups, omit REMOTE_HOST
                if addr is None:  # sometimes this can happen
                    # figure out if AF_INET or AF_INET6.
                    if len(s.getsockname()) == 2:
                        # AF_INET
                        addr = ('0.0.0.0', 0)
                    else:
                        # AF_INET6
                        addr = ('::', 0)
                conn.remote_addr = addr[0]
                conn.remote_port = addr[1]

            conn.ssl_env = ssl_env
            return conn

        except socket.timeout:
            # The only reason for the timeout in start() is so we can
            # notice keyboard interrupts on Win32, which don't interrupt
            # accept() by default
            return
        except socket.error as ex:
            if self.server.stats['Enabled']:
                self.server.stats['Socket Errors'] += 1
            if ex.args[0] in errors.socket_error_eintr:
                # I *think* this is right. EINTR should occur when a signal
                # is received during the accept() call; all docs say retry
                # the call, and I *think* I'm reading it right that Python
                # will then go ahead and poll for and handle the signal
                # elsewhere. See
                # https://github.com/cherrypy/cherrypy/issues/707.
                return
            if ex.args[0] in errors.socket_errors_nonblocking:
                # Just try again. See
                # https://github.com/cherrypy/cherrypy/issues/479.
                return
            if ex.args[0] in errors.socket_errors_to_ignore:
                # Our socket was closed.
                # See https://github.com/cherrypy/cherrypy/issues/686.
                return
            raise

    def close(self):
        """Close all monitored connections."""
        for conn in self._readable_conns:
            conn.close()
        self._readable_conns.clear()

        for _, conn in self._get_selector_conns():
            conn.close()

        # server closes its own socket
        self._ctrl_tx.close()
        self._ctrl_rx.close()
        self._selector.close()

    @property
    def can_add_keepalive_connection(self):
        """Flag whether it is allowed to add a new keep-alive connection."""
        ka_limit = self.server.keep_alive_conn_limit
        return ka_limit is None or self._num_conns < ka_limit
