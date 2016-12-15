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
    except OSError as e:
        return e.errno == os.errno.EPERM
    else:
        return True


class IDJCMonitor(gobject.GObject):
    """Monitor IDJC internals relating to a specific profile or session.
    
    Can yield information about streams, music metadata, health.
    example usage: http://idjc.sourceforge.net/code_idjcmon.html
    """
    
    __gsignals__ = {
        'launch' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                    (gobject.TYPE_STRING, gobject.TYPE_UINT)),
        'quit' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                    (gobject.TYPE_STRING, gobject.TYPE_UINT)),
        'streamstate-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_INT, gobject.TYPE_BOOLEAN, gobject.TYPE_STRING)),
        'recordstate-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_INT, gobject.TYPE_BOOLEAN, gobject.TYPE_STRING)),
        'channelstate-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (gobject.TYPE_UINT, gobject.TYPE_BOOLEAN)),
        'voip-mode-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                 (gobject.TYPE_UINT,)),
        'metadata-changed' : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                                    (gobject.TYPE_STRING,) * 5),
        'effect-started': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                           (gobject.TYPE_STRING,) * 2 + (gobject.TYPE_UINT,)),
        'effect-stopped': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                           (gobject.TYPE_UINT,)),
        'tracks-finishing': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                            ()),
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
        'music-filename' : (gobject.TYPE_STRING, 'music_filename',
                            'the audio file pathname of the track',
                            "", gobject.PARAM_READABLE),
        'streaminfo' : (gobject.TYPE_PYOBJECT, 'streaminfo',
                'information about the streams', gobject.PARAM_READABLE),
        'recordinfo' : (gobject.TYPE_PYOBJECT, 'recordinfo',
                'information about the recorders', gobject.PARAM_READABLE),
        'channelinfo' : (gobject.TYPE_PYOBJECT, 'channelinfo',
                'toggle state of the audio channels', gobject.PARAM_READABLE),
        'voip-mode' : (gobject.TYPE_UINT, 'voip-mode', 'voice over ip mixer mode',
                                                0, 2, 0, gobject.PARAM_READABLE)
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
            self.__main.connect_to_signal("effect_started",
                                                self._effect_started_handler)
            self.__main.connect_to_signal("effect_stopped",
                                                self._effect_stopped_handler)
            self.__main.connect_to_signal("quitting", self._quit_handler)
            self.__main.connect_to_signal("heartbeat", self._heartbeat_handler)
            self.__main.connect_to_signal("channelstate_changed",
                                                    self._channelstate_handler)
            self.__main.connect_to_signal("voip_mode_changed",
                                                    self._voip_mode_handler)
            self.__main.connect_to_signal("tracks_finishing",
                                                self._tracks_finishing_handler)
            self.__output.connect_to_signal("streamstate_changed",
                                                    self._streamstate_handler)
            self.__output.connect_to_signal("recordstate_changed",
                                                    self._recordstate_handler)

            # Start watchdog thread.
            self.__watchdog_id = gobject.timeout_add_seconds(3, self._watchdog)

            self.__streams = {n : (False, "unknown") for n in xrange(10)}
            self.__recorders = {n : (False, "unknown") for n in xrange(4)}
            self.__channels = [False] * 12
            self.__voip_mode = 0
            main_iface = dbus.Interface(self.__main, self.__base_interface)
            output_iface = dbus.Interface(self.__output, self.__base_interface)
            
            self.emit("launch", self.__profile, self.__pid)
            
            # Tell IDJC to initialize as empty its cache of sent data.
            # This yields a dump of server related info.
            main_iface.new_plugin_started()
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

                for id_, (rec, where) in self.__recorders.iteritems():
                    if rec:
                        self._recordstate_handler(id_, 0, where)
                        
                for index, open_ in enumerate(self.__channels):
                    if open_:
                        self._channelstate_handler(index, 0)
                
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

    def _recordstate_handler(self, numeric_id, recording, where):
        numeric_id = int(numeric_id)
        recording = bool(recording)
        where = where.encode("utf-8")
        self.__recorders[numeric_id] = (recording, where)
        self.notify("recordinfo")
        self.emit("recordstate-changed", numeric_id, recording, where)

    def _channelstate_handler(self, numeric_id, open_):
        numeric_id = int(numeric_id)
        open_ = bool(open_)
        self.__channels[numeric_id] = open_
        self.notify("channelinfo")
        self.emit("channelstate-changed", numeric_id, open_)

    def _voip_mode_handler(self, mode):
        mode = int(mode)
        self.__voip_mode = mode
        self.notify("voip-mode")
        self.emit("voip-mode-changed", mode)

    def _tracks_finishing_handler(self):
        self.emit("tracks-finishing")

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

    def _effect_started_handler(self, title, pathname, player):
        self.emit("effect-started", title, pathname, player)

    def _effect_stopped_handler(self, player):
        self.emit("effect-stopped", player)

    def do_get_property(self, prop):
        if self.__shutdown:
            raise AttributeError(
                        "Attempt to read property after shutdown was called.")
        
        name = prop.name
        
        if name in ("artist", "title", "album", "songname", "music_filename",
                    "effect_pathname"):
            return getattr(self, "_IDJCMonitor__" + name)
        if name == "streaminfo":
            return tuple(self.__streams[n] for n in xrange(10))
        elif name == "recordinfo":
            return tuple(self.__recorders[n] for n in xrange(4))
        elif name == "channelinfo":
            return tuple(self.__channels[n] for n in xrange(12))
        elif name == "voip-mode":
            return self.__voip_mode
        else:
            raise AttributeError("Unknown property %s in %s" % (
                                                            name, repr(self)))

    def notify(self, property_name):
        if not self.__shutdown:
            gobject.GObject.notify(self, property_name)
            
    def emit(self, *args, **kwargs):
        if not self.__shutdown:
            gobject.GObject.emit(self, *args, **kwargs)
