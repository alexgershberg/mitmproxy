from __future__ import absolute_import
import copy
import netlib.tcp
from .. import stateobject, utils, version
from ..proxy.connection import ClientConnection, ServerConnection


KILL = 0  # const for killed requests


class Error(stateobject.SimpleStateObject):
    """
        An Error.

        This is distinct from an HTTP error response (say, a code 500), which
        is represented by a normal Response object. This class is responsible
        for indicating errors that fall outside of normal HTTP communications,
        like interrupted connections, timeouts, protocol errors.

        Exposes the following attributes:

            flow: Flow object
            msg: Message describing the error
            timestamp: Seconds since the epoch
    """
    def __init__(self, msg, timestamp=None):
        """
        @type msg: str
        @type timestamp: float
        """
        self.flow = None  # will usually be set by the flow backref mixin
        self.msg = msg
        self.timestamp = timestamp or utils.timestamp()

    _stateobject_attributes = dict(
        msg=str,
        timestamp=float
    )

    def __str__(self):
        return self.msg

    @classmethod
    def _from_state(cls, state):
        f = cls(None)  # the default implementation assumes an empty constructor. Override accordingly.
        f._load_state(state)
        return f

    def copy(self):
        c = copy.copy(self)
        return c


class Flow(stateobject.SimpleStateObject):
    def __init__(self, conntype, client_conn, server_conn, live=None):
        self.conntype = conntype
        self.client_conn = client_conn
        """@type: ClientConnection"""
        self.server_conn = server_conn
        """@type: ServerConnection"""
        self.live = live  # Used by flow.request.set_url to change the server address
        """@type: LiveConnection"""

        self.error = None
        """@type: Error"""
        self._backup = None

    _stateobject_attributes = dict(
        error=Error,
        client_conn=ClientConnection,
        server_conn=ServerConnection,
        conntype=str
    )

    def _get_state(self):
        d = super(Flow, self)._get_state()
        d.update(version=version.IVERSION)
        return d

    def __eq__(self, other):
        return self is other

    def copy(self):
        f = copy.copy(self)

        f.client_conn = self.client_conn.copy()
        f.server_conn = self.server_conn.copy()

        if self.error:
            f.error = self.error.copy()
        return f

    def modified(self):
        """
            Has this Flow been modified?
        """
        if self._backup:
            return self._backup != self._get_state()
        else:
            return False

    def backup(self, force=False):
        """
            Save a backup of this Flow, which can be reverted to using a
            call to .revert().
        """
        if not self._backup:
            self._backup = self._get_state()

    def revert(self):
        """
            Revert to the last backed up state.
        """
        if self._backup:
            self._load_state(self._backup)
            self._backup = None


class ProtocolHandler(object):
    def __init__(self, c):
        self.c = c
        """@type: libmproxy.proxy.server.ConnectionHandler"""
        self.live = LiveConnection(c)
        """@type: LiveConnection"""

    def handle_messages(self):
        """
        This method gets called if a client connection has been made. Depending on the proxy settings,
        a server connection might already exist as well.
        """
        raise NotImplementedError  # pragma: nocover

    def handle_server_reconnect(self, state):
        """
        This method gets called if a server connection needs to reconnect and there's a state associated
        with the server connection (e.g. a previously-sent CONNECT request or a SOCKS proxy request).
        This method gets called after the connection has been restablished but before SSL is established.
        """
        raise NotImplementedError  # pragma: nocover

    def handle_error(self, error):
        """
        This method gets called should there be an uncaught exception during the connection.
        This might happen outside of handle_messages, e.g. if the initial SSL handshake fails in transparent mode.
        """
        raise error  # pragma: nocover


class LiveConnection(object):
    """
    This facade allows protocol handlers to interface with a live connection,
    without requiring the expose the ConnectionHandler.
    """
    def __init__(self, c):
        self.c = c
        self._backup_server_conn = None
        """@type: libmproxy.proxy.server.ConnectionHandler"""

    def change_server(self, address, ssl=False, force=False, persistent_change=False):
        address = netlib.tcp.Address.wrap(address)
        if force or address != self.c.server_conn.address or ssl != self.c.server_conn.ssl_established:

            self.c.log("Change server connection: %s:%s -> %s:%s [persistent: %s]" % (
                self.c.server_conn.address.host,
                self.c.server_conn.address.port,
                address.host,
                address.port,
                persistent_change
            ), "debug")

            if self._backup_server_conn:
                self._backup_server_conn = self.c.server_conn
                self.c.server_conn = None
            else:  # This is at least the second temporary change. We can kill the current connection.
                self.c.del_server_connection()

            self.c.set_server_address(address)
            self.c.establish_server_connection(ask=False)
            if ssl:
                self.c.establish_ssl(server=True)
        if persistent_change:
            self._backup_server_conn = None

    def restore_server(self):
        # TODO: Similar to _backup_server_conn, introduce _cache_server_conn, which keeps the changed connection open
        # This may be beneficial if a user is rewriting all requests from http to https or similar.
        if not self._backup_server_conn:
            return

        self.c.log("Restore original server connection: %s:%s -> %s:%s" % (
            self.c.server_conn.address.host,
            self.c.server_conn.address.port,
            self._backup_server_conn.address.host,
            self._backup_server_conn.address.port
        ), "debug")

        self.c.del_server_connection()
        self.c.server_conn = self._backup_server_conn
        self._backup_server_conn = None