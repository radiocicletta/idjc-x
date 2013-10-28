# idjcmonitor.py (C) 2013 Stephen Fairchild
# Released under the GNU Lesser General Public License version 2.0 (or
# at your option, any later version).

"""A monitoring class that keeps an eye on IDJC.

It can be extended to issue e-mail alerts if IDJC freezes or perform Twitter
updates when the music changes.

Requires IDJC 0.8.9 or higher.

Example usage: http://idjc.sourceforge.net/code_idjcmon.html
"""

import os
import sys
import time

import gobject
import dbus
from dbus.mainloop.glib import DBusGMainLoop


__all__ = ["IDJCMonitor"]


BUS_BASENAME = "net.sf.idjc"
OBJ_BASENAME = "/net/sf/idjc"


def pid_exists(pid):
    """Check whether pid exists in the current process table."""

    if pid < 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError, e:
        return e.errno == errno.EPERM
    else:
        return True


class IDJCMonitor(gobject.GObject):
    """Monitor IDJC internals relating to a specific profile or session.
    
    Can obtain information about where streaming to or the music metadata.
    This info can then be published whereever without having to touch
    the IDJC source code and is therefore easy to maintain.
    """
    
    __gsignals__ = {
        'launch' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                    (gobject.TYPE_STRING, gobject.TYPE_UINT)),
        'quit' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                    (gobject.TYPE_STRING, gobject.TYPE_UINT)),
        'streamstate-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_INT, gobject.TYPE_BOOLEAN, gobject.TYPE_STRING)),
        
        'metadata-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                                    (gobject.TYPE_STRING,) * 5),
        'frozen' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_STRING, gobject.TYPE_UINT, gobject.TYPE_BOOLEAN))
    }
    
    __gproperties__ = {
        'artist' : (gobject.TYPE_STRING, 'artist', 'artist from track metadata',
                                                    "", gobject.PARAM_READABLE),
        'title' : (gobject.TYPE_STRING, 'title', 'title from track metadata',
                                                    "", gobject.PARAM_READABLE),
        'album' : (gobject.TYPE_STRING, 'album', 'album from track metadata',
                                                    "", gobject.PARAM_READABLE),
        'songname' : (gobject.TYPE_STRING, 'songname',
                            'the song name from metadata tags when available'
                            ' and from the filenmame when not',
                            "", gobject.PARAM_READABLE),
        'music_filename' : (gobject.TYPE_STRING, 'music_filename',
                            'music_filename from track metadata',
                            "", gobject.PARAM_READABLE),
        'streaminfo' : (gobject.TYPE_PYOBJECT, 'streaminfo',
                'information about the streams', gobject.PARAM_READABLE)
    }
    
    def __init__(self, profile):
        """Takes the profile parameter e.g. "default".
        
        Can also handle sessions with "session.sessionname"
        """
        
        gobject.GObject.__init__(self)
        self.__profile = profile
        self.__bus = dbus.SessionBus(mainloop=DBusGMainLoop())
        self.__bus_address = ".".join((BUS_BASENAME, profile))
        self.__base_objpath = OBJ_BASENAME
        self.__base_interface = BUS_BASENAME
        self.__artist = self.__title = self.__album = ""
        self.__songname = self.__music_filename = ""
        self.__shutdown = False
        self._start_probing()

    @property
    def main(self):
        """A DBus interface to the main object.
        
        Code that uses this should catch any AttributeError exceptions.
        """
        return dbus.Interface(self.__main, self.__base_interface)
        
    @property
    def output(self):
        """A DBus interface to the output object.
        
        Code that uses this should catch any AttributeError exceptions.
        """
        return dbus.Interface(self.__output, self.__base_interface)

    @property
    def controls(self):
        """A DBus interface to the controls object.
        
        Code that uses this should catch any AttributeError exceptions.
        """
        return dbus.Interface(self.__controls, self.__base_interface)
        
    def shutdown(self):
        """Block both signal emission and property reads."""
        
        self.__shutdown = True

    def _start_probing(self):
        self.__watchdog_id = None
        self.__probe_id = None
        self.__watchdog_notice = False
        self.__pid = 0
        self.__frozen = False
        self.__main = self.__output = self.__controls = None
        if not self.__shutdown:
            self.__probe_id = gobject.timeout_add_seconds(
                                                2, self._idjc_started_probe)

    def _idjc_started_probe(self):
        # Check for a newly started IDJC instance of the correct profile.
        
        bus_address = ".".join((BUS_BASENAME, self.__profile))
        
        try:
            self.__main = self.__bus.get_object(self.__bus_address,
                                                self.__base_objpath + "/main")
            self.__output = self.__bus.get_object(self.__bus_address,
                                                self.__base_objpath + "/output")
            self.__controls = self.__bus.get_object(self.__bus_address,
                                            self.__base_objpath + "/controls")

            main_iface = dbus.Interface(self.__main, self.__base_interface)
            main_iface.pid(reply_handler=self._pid_reply_handler,
                            error_handler=self._pid_error_handler)
        except dbus.exceptions.DBusException:
            # Keep searching periodically.
            return not self.__shutdown
        else:
            return False

    def _pid_reply_handler(self, value):
        self.__pid = value
        try:
            self.__main.connect_to_signal("track_metadata_changed",
                                                        self._metadata_handler)
            self.__main.connect_to_signal("quitting", self._quit_handler)
            self.__main.connect_to_signal("heartbeat", self._heartbeat_handler)
            self.__output.connect_to_signal("streamstate_changed",
                                                    self._streamstate_handler)

            # Start watchdog thread.
            self.__watchdog_id = gobject.timeout_add_seconds(3, self._watchdog)

            self.__streams = {n : (False, "unknown") for n in xrange(10)}
            output_iface = dbus.Interface(self.__output, self.__base_interface)
            
            self.emit("launch", self.__profile, self.__pid)
            
            # Tell IDJC to initialize as empty its cache of sent data.
            # This yields a dump of server related info.
            output_iface.new_plugin_started()
        except dbus.exceptions.DBusException:
            self._start_probing()

    def _pid_error_handler(self, error):
        self._start_probing()

    def _watchdog(self):
        if self.__watchdog_notice:
            if pid_exists(int(self.__pid)):
                if not self.__frozen:
                    self.__frozen = True
                    self.emit("frozen", self.__profile, self.__pid, True)
                return True
            else:
                for id_, (conn, where) in self.__streams.iteritems():
                    if conn:
                        self._streamstate_handler(id_, 0, where)
                self._quit_handler()
                return False
        elif self.__frozen:
            self.__frozen = False
            self.emit("frozen", self.__profile, self.__pid, False)

        self.__watchdog_notice = True
        return not self.__shutdown

    def _heartbeat_handler(self):
        self.__watchdog_notice = False

    def _quit_handler(self):
        """Start scanning for a new bus object."""

        if self.__watchdog_id is not None:
            gobject.source_remove(self.__watchdog_id)
            self.emit("quit", self.__profile, self.__pid)
        self._start_probing()
        
    def _streamstate_handler(self, numeric_id, connected, where):
        numeric_id = int(numeric_id)
        connected = bool(connected)
        where = where.encode("utf-8")
        self.__streams[numeric_id] = (connected, where)
        self.notify("streaminfo")
        self.emit("streamstate-changed", numeric_id, connected, where)

    def _metadata_handler(self, artist, title, album, songname, music_filename):

        def update_property(name, value):
            oldvalue = getattr(self, "_IDJCMonitor__" + name)
            newvalue = value.encode("utf-8")
            if newvalue != oldvalue:
                setattr(self, "_IDJCMonitor__" + name, newvalue)
                self.notify(name)

        for name, value in zip(
                        "artist title album songname music_filename".split(),
                            (artist, title, album, songname, music_filename)):
            update_property(name, value)

        self.emit("metadata-changed", self.__artist, self.__title,
                                                self.__album, self.__songname,
                                                self.__music_filename)

    def do_get_property(self, prop):
        if self.__shutdown:
            raise AttributeError(
                        "Attempt to read property after shutdown was called.")
        
        name = prop.name
        
        if name in ("artist", "title", "album", "songname", "music_filename"):
            return getattr(self, "_IDJCMonitor__" + name)
        if name == "streaminfo":
            return tuple(self.__streams[n] for n in xrange(10))
        else:
            raise AttributeError("Unknown property %s in %s" % (
                                                            name, repr(self)))

    def notify(self, property_name):
        if not self.__shutdown:
            gobject.GObject.notify(self, property_name)
            
    def emit(self, *args, **kwargs):
        if not self.__shutdown:
            gobject.GObject.emit(self, *args, **kwargs)
