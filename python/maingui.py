#   maingui.py: Main python code of IDJC
#   Copyright 2005-2011 Stephen Fairchild (s-fairchild@users.sourceforge.net)
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program in the file entitled COPYING.
#   If not, see <http://www.gnu.org/licenses/>.


import os
import sys
import subprocess
import configparser
import socket
import pickle
import signal
import time
import gettext
import itertools
import collections
import json
import uuid
import ctypes
from binascii import hexlify, unhexlify

import dbus
import dbus.service
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import Pango

from idjc import FGlobs, PGlobs
from .playergui import *
from .sourceclientgui import *
from .preferences import *
from .jingles import ExtraPlayers
from .utils import SlotObject
from .utils import LinkUUIDRegistry
from .utils import PathStr
from .gtkstuff import threadslock, WindowSizeTracker, ConfirmationDialog
from .gtkstuff import IconChooserButton, IconPreviewFileChooserDialog, LEDDict
from .gtkstuff import LabelSubst, gdklock, nullcm
from .gtkstuff import idle_add, timeout_add, timeout_add_seconds, source_remove
from . import midicontrols
from .tooltips import set_tip
from . import songdb
from .prelims import *


_ = gettext.translation(
    FGlobs.package_name,
    FGlobs.localedir,
    fallback=True).gettext

args = ArgumentParserImplementation().parse_args()
pm = ProfileManager()
link_uuid_reg = LinkUUIDRegistry()

METER_TEXT_SIZE = 8000


class FreewheelButton(Gtk.Button):
    LED = LEDDict(9)

    def __init__(self, mixer_write):
        super(FreewheelButton, self).__init__()
        hbox = Gtk.Box()
        self._indicator = Gtk.Image()
        self._indicator.set_alignment(0.0, 0.0)
        hbox.pack_start(self._indicator, False, False, 0)
        self._indicator.show()
        label = Gtk.Label()
        label.set_padding(2, 0)
        label.set_alignment(1.0, 0.5)
        label.set_markup("<span size='15000'>\u2699</span>")
        hbox.pack_start(label, False, False, 0)
        label.show()
        self.add(hbox)
        hbox.show()
        self._mixer_write = mixer_write
        self.connect("clicked", lambda w: self._cb_toggle())
        self._enabler = Gtk.CheckButton(
            _('Show a JACK freewheel control on the main panel')
        )
        self._enabler.connect("toggled", self._cb_enabler)
        set_tip(self, _('Toggle JACK freewheel mode.'))
        self._active = None
        self.set_meter_value(False)

    def _cb_toggle(self):
        self._mixer_write("ACTN=freewheel_toggle\nend\n")

    def set_active(self, active):
        self._mixer_write(
            "ACTN=freewheel_%s\nend\n" % ("on" if active else "off")
        )

    def _cb_enabler(self, widget):
        self.set_visible(widget.get_active())

    @property
    def enabler(self):
        """This button has a show/hide control in prefs."""

        return self._enabler

    @property
    def activedict(self):
        """Info for save/restore."""

        return {"freewheel_button_enable": self._enabler}

    def set_meter_value(self, active):
        """Indicator of freewheel mode to be set using this method."""

        if active != self._active:
            self._active = active
            self._indicator.set_from_pixbuf(
                self.LED["red" if active else "clear"]
            )


class MenuMixin(object):

    def build(self, menu, autowipe=False, use_underline=True):
        def mkitems(x, how=Gtk.MenuItem):
            for name, text in x:
                mi = how(text)
                mi.set_use_underline(use_underline)
                menu.append(mi)
                mi.show()
                setattr(self, name + "menu_i", mi)
                if autowipe:
                    mi.connect("activate", self.cb_autowipe)

                if issubclass(how, Gtk.CheckMenuItem) and \
                        use_underline is True:
                    a = Gtk.ToggleAction(None, text, None, None)
                    mi.set_related_action(a)
                    setattr(self, name + "menu_a", a)

        return mkitems

    def submenu(self, mi, name):
        m = Gtk.Menu()
        mi.set_submenu(m)
        m.show()
        setattr(self, name + "menu", m)
        return m

    def sep(self, menu):
        s = Gtk.SeparatorMenuItem()
        menu.append(s)
        s.show()

    def cb_autowipe(self, mi):
        mi.get_submenu().foreach(lambda w: w.destroy())


class MainMenu(Gtk.MenuBar, MenuMixin):

    def __init__(self):
        super(MainMenu, self).__init__()

        self.build(self)(((
            "file", _('File')), ("view", _('View')),
            ("jack", _('JACK Ports')), ("help", _('Help'))))
        self.submenu(self.filemenu_i, "file")
        self.build(self.filemenu, autowipe=True)(((
            "streams", _('Streams')),
            ("recorders", _('Recorders'))))

        self.sep(self.filemenu)
        self.build(self.filemenu)(
            (("quit", Gtk.STOCK_QUIT),),
            Gtk.ImageMenuItem)

        for each in ("streams", "recorders"):
            mi = getattr(self, each + "menu_i")
            self.submenu(mi, each)

        self.submenu(self.viewmenu_i, "view")
        mkitems = self.build(self.viewmenu)
        mkitems(list(zip("output prefs profiles".split(" "),
                (_('Output'), _('Preferences'), _('Profiles')))))
        self.sep(self.viewmenu)
        mkitems(list(
            zip("songdb chmeters strmeters players backgroundtracks buttonbar".split(" "),
                (_('Music Database'), _('Channel Meters'), _('Output Meters'),
                 _('Tabbed Area'), _('Background Tracks'), _('Button Bar')))),
                Gtk.CheckMenuItem)

        if not songdb.have_songdb:
            self.songdbmenu_i.hide()

        self.submenu(self.jackmenu_i, "jack")

        self.submenu(self.helpmenu_i, "help")
        self.build(self.helpmenu)((("about", Gtk.STOCK_ABOUT),),
                                  Gtk.ImageMenuItem)

        self.filemenu_i.connect("activate", self.cb_filemenu_activate)

    def cb_filemenu_activate(self, menuitem):
        self.streamsmenu_i.emit("activate")
        self.recordersmenu_i.emit("activate")


class JackMenu(MenuMixin):

    def __init__(self, menu, write, read):
        self.menu = menu
        self.write = write
        self.read = read
        self.ports = []
        self.pathname = pm.ports_pathname
        self.session_type = pm.session_type

        # pylint: disable=E1103
        #
        # member really exists, was created by setattr

        mkitems = self.build(menu.jackmenu)
        mkitems(
            list(
                zip(
                    "channels players voip dsp mix output other".split(),
                    (
                        _('Channels'), _('Players'),
                        _('VoIP'), _('DSP'), _('Mix'), _('Output'), _('Misc'))
                )
            )
        )
        self.submenu(self.channelsmenu_i, "channels")
        self.submenu(self.playersmenu_i, "players")
        self.submenu(self.voipmenu_i, "voip")
        self.submenu(self.dspmenu_i, "dsp")
        self.submenu(self.mixmenu_i, "mix")
        self.submenu(self.outputmenu_i, "output")
        self.submenu(self.othermenu_i, "other")

        out2_in2 = itertools.cycle(("_out_",) * 2 + ("_in_",) * 2)
        lr = itertools.cycle("lr")
        dj2_str2 = itertools.cycle(("dj",) * 2 + ("str",) * 2)

        for prefix in "pl pr pi".split():
            for each in zip((prefix,) * 4, out2_in2, lr):
                self.add_port(self.playersmenu, "".join(each))

        for prefix in "pe01-12 pe13-24".split():
            for each in zip((prefix,) * 2, ("_out_", ) * 2, lr):
                self.add_port(self.playersmenu, "".join(each))
        for each in zip(("pe_in_", ) * 2, lr):
            self.add_port(self.playersmenu, "".join(each))

        for each in zip(("voip",) * 4, out2_in2, lr):
            self.add_port(self.voipmenu, "".join(each))

        for each in zip(("dsp",) * 4, out2_in2, lr):
            self.add_port(self.dspmenu, "".join(each))

        for each in zip(dj2_str2, ("_out_",) * 4, lr):
            self.add_port(self.mixmenu, "".join(each))

        for i in range(1, PGlobs.num_micpairs * 2 + 1):
            self.add_port(self.channelsmenu, "ch_in_" + str(i))

        for each in zip(("output_in_",) * 2, lr):
            self.add_port(self.outputmenu, "".join(each))

        self.add_port(self.othermenu, "midi_control")
        self.add_port(self.othermenu, "alarm_out")

        self._port_data = []

        self.sep(menu.jackmenu)
        mkitems((("reset", _('Reset')),))
        self.resetmenu_i.connect("activate", self._reset_confirm_dialog)
        set_tip(
            self.resetmenu_i,
            _('Reset the JACK port connections to the default settings.')
        )

    def _reset_confirm_dialog(self, menuitem):
        dialog = ConfirmationDialog(
            "",
            _('<span size="12000" weight="bold">Reset all JACK port connections?</span>\n\n'
              'All currently established connections will be lost\n'
              'and replaced with defaults.'),
            markup=True,
            action=Gtk.STOCK_YES,
            inaction=Gtk.STOCK_NO)
        dialog.set_transient_for(self.menu.get_toplevel())
        dialog.ok.connect("clicked", lambda w: self._reset_port_connections())
        dialog.show_all()

    def _reset_port_connections(self):
        for port in self.ports:
            self.write("disconnect", "JPRT=%s\nJPT2=\nend\n" % port)
        self.load(where="")

    def add_port(self, menu, port):
        pport = os.environ["client_id"] + ":" + port
        self.ports.append(pport)
        self.build(menu, autowipe=True, use_underline=False)(((port, pport),))
        mi = getattr(self, port + "menu_i")
        sub = self.submenu(mi, port)
        mi.connect("activate", self.cb_port_connections, pport, sub)
        mi.emit("activate")

    def cb_port_connections(self, mi, port, menu):
        reply = ""

        if "_in_" in port or port.endswith("_in"):
            filter_ = "outputs"
        elif "_out_" in port or port.endswith("_out"):
            filter_ = "inputs"
        elif "midi" in port:
            filter_ = "midioutputs"
        else:
            print("JackMenu.port_connections: unknown port type")
            return

        self.write("portread", "JFIL=%s\nJPRT=%s\nend\n" % (filter_, port))
        while not reply.startswith("jackports="):
            reply = self.read()
        reply = reply[10:].rstrip().split()
        if not reply:
            self.build(menu)((("noports",
                               _('No compatible ports available.')),))
            self.noportsmenu_i.set_sensitive(False)
        else:
            for destport in reply:
                self.build(menu, use_underline=False)(
                    (("targetport", unhexlify(destport[1:]).decode()),),
                    how=Gtk.CheckMenuItem)
                mi = getattr(self, "targetportmenu_i")
                if destport.startswith("@"):
                    mi.set_active(True)
                mi.connect(
                    "activate",
                    self.cb_activate,
                    port,
                    unhexlify(destport[1:]).decode())

    def cb_activate(self, mi, local, dest):
        cmd = "connect" if mi.get_active() else "disconnect"
        self.write(cmd, "JPRT=%s\nJPT2=%s\nend\n" % (local, dest))
        # Defer save until backend reports connections have changed.

    def get_playback_port_qty(self):
        self.write("portread", "JFIL=\nJPRT=\nend\n")
        reply = ""
        while not reply.startswith("jackports="):
            reply = self.read()

        match = b"-" + hexlify("system:playback_".encode())
        pbports = [x for x in reply[10:-1]
                   .split() if x.startswith(match.decode())]
        return len(pbports)

    def standard_save(self):
        self._port_data = self._get_port_data()

        if self.session_type == "L0":
            self._save(self._port_data)

    def session_save(self, where=None):
        self._port_data = self._get_port_data()

        self._save(self._port_data, where)

        if pm.profile is not None:
            arg = _("{0} profile={1}:{2} settings saved.").format(
                PGlobs.app_shortform, self.session_type, pm.profile)
        else:
            arg = _("{0} session={1}:{2} settings saved.").format(
                PGlobs.app_shortform, self.session_type, pm.session_name)

        try:
            subprocess.call(["notify-send", arg])
        except OSError:
            pass

    def _get_port_data(self):
        total = []
        for port in self.ports:
            element = [port]
            self.write("portread", "JFIL=\nJPRT=%s\nend\n" % port)
            reply = ""
            while not reply.startswith("jackports="):
                reply = self.read()

            element.append([
                unhexlify(x.lstrip("@-")).decode()
                for x in reply[10:-1].split()
                if x.startswith("@")
            ])
            total.append(element)
        return total

    def _save(self, data, where=None):
        if where is not None:
            where = os.path.join(where, os.path.split(self.pathname)[1])
        client_id = "\"%s:" % os.environ["client_id"]
        try:
            with open(where or self.pathname, "w") as f:
                f.write(json.dumps(data).replace(client_id, "\"{client_id}:"))
        except Exception:
            print("problem writing", self.pathname)
        else:
            print("jack connections saved")

    def load(self, where=None, startup=False):
        try:
            where = self.pathname if where is None else where
            with open(where) as f:
                cons = f.read()
        except Exception:
            if where is not None:
                print("problem reading JACK connections files,", where)
            try:
                if startup and args.no_default_jack_connections:
                    cons = "[]"
            except Exception:
                cons = "[]"
            else:
                cons = """[
                    ["{client_id}:pl_out_l", ["{client_id}:pl_in_l"]],
                    ["{client_id}:pl_out_r", ["{client_id}:pl_in_r"]],
                    ["{client_id}:pr_out_l", ["{client_id}:pr_in_l"]],
                    ["{client_id}:pr_out_r", ["{client_id}:pr_in_r"]],
                    ["{client_id}:pi_out_l", ["{client_id}:pi_in_l"]],
                    ["{client_id}:pi_out_r", ["{client_id}:pi_in_r"]],
                    ["{client_id}:pe01-12_out_l", ["{client_id}:pe_in_l"]],
                    ["{client_id}:pe01-12_out_r", ["{client_id}:pe_in_r"]],
                    ["{client_id}:pe13-24_out_l", ["{client_id}:pe_in_l"]],
                    ["{client_id}:pe13-24_out_r", ["{client_id}:pe_in_r"]],
                    ["{client_id}:ch_in_1", ["system:capture_1"]],
                    ["{client_id}:ch_in_2", ["system:capture_2"]],
                    ["{client_id}:dj_out_l", ["system:playback_1"]],
                    ["{client_id}:dj_out_r", ["system:playback_2"]],
                    ["{client_id}:alarm_out", ["system:playback_1", "system:playback_2"]],
                    ["{client_id}:output_in_l", ["{client_id}:str_out_l"]],
                    ["{client_id}:output_in_r", ["{client_id}:str_out_r"]], """

                if self.get_playback_port_qty() < 8:
                    cons += """
                    ["{client_id}:str_out_l",
                        ["system:playback_3", "{client_id}:output_in_l"]],
                    ["{client_id}:str_out_r",
                        ["system:playback_4", "{client_id}:output_in_r"]]] """
                else:
                    cons += """
                    ["{client_id}:str_out_l",
                        ["system:playback_5", "{client_id}:output_in_l"]],
                    ["{client_id}:str_out_r",
                        ["system:playback_6", "{client_id}:output_in_r"]]] """
                print(cons)

        try:
            cons = json.loads(cons.format(client_id=os.environ["client_id"]))
        except ValueError:
            print("jack port connections file is empty")
        else:
            self._port_data = cons
            try:
                if not startup or not args.no_jack_connections:
                    self.restore(cons)
            except AttributeError:
                self.restore(cons)

    def restore(self, cons=None, restrict=""):
        cons = cons or self._port_data
        for port, targets in cons:
            for target in targets:
                if port.startswith(restrict):
                    self.write("connect", "JPRT=%s\nJPT2=%s\nend\n" %
                               (port, target))


class ColouredArea(Gtk.DrawingArea):

    def __init__(self, colour=Gdk.RGBA(255, 255, 255, 0)):
        super(ColouredArea, self).__init__()
        self.colour = colour
        self.rect = Gdk.Rectangle()
        self.connect("configure-event", self._on_configure)
        self.connect("draw", self._on_expose)

    def set_colour(self, colour):
        self.colour = colour
        self.queue_draw_area(0, 0, self.rect.width, self.rect.height)

    def _on_configure(self, widget, event):
        self.rect.width = event.width
        self.rect.height = event.height

    def _on_expose(self, widget, event):
        if not self.get_window():
            return
        ctx = Gdk.cairo_create(self.get_window())
        Gdk.cairo_set_source_color(ctx, self.colour)
        ctx.rectangle(0, 0, self.rect.width, self.rect.height)
        ctx.fill()


class ColourButton(Gtk.ColorButton):

    def __init__(self, colour=None):

        super(ColourButton, self).__init__()
        if colour and isinstance(colour, Gdk.RGBA):
            self.set_rgba(colour)
        elif colour and isinstance(colour, Gdk.Color):
            self.set_color(colour)

    def get_text(self):
        return self.get_rgba().to_string()

    def set_text(self, string):
        colour = Gdk.RGBA()
        colour.parse(string)
        if colour and isinstance(colour, Gdk.RGBA):
            self.set_rgba(colour)
        elif colour and isinstance(colour, Gdk.Color):
            self.set_color(colour)


class IconChooserButtonExtd(IconChooserButton):

    def get_text(self):
        return self.get_filename() or ""

    def set_text(self, filename):
        self.set_filename(filename or None)


class MicButton(Gtk.ToggleButton):

    @property
    def flash(self):
        return self.__flash

    @flash.setter
    def flash(self, value):
        self.__flash = bool(value) and self.has_reminder_flash()
        self.__indicate()

    @staticmethod
    def __cb_toggle(self):
        self.__indicate()
        if self.get_active():
            self.set_colour(self.open_colour)
            self.opener_tab.button_was_on = True
        else:
            self.opener_tab.button_was_on = False

    def __indicate(self):
        if self.get_active():
            if self.flash:
                self.set_colour(self.flash_colour)
            else:
                self.set_colour(self.open_colour)
        else:
            self.set_colour(self.closed_colour)

    def set_colour(self, colour):
        for each in (self.ca1, self.ca2):
            each.set_colour(colour)

    def __init__(self, opener_settings, opener_tab, mic_agc_list):
        super(MicButton, self).__init__()

        self.opener_tab = opener_tab

        nsa = not opener_settings.button_numbers.get_active()

        self.open_colour = opener_settings.open_colour.get_color()
        self.closed_colour = opener_settings.closed_colour.get_color()
        self.flash_colour = opener_settings.reminder_colour.get_color()
        self.has_reminder_flash = opener_tab.has_reminder_flash.get_active

        hbox = Gtk.Box()
        hbox.set_spacing(4)

        def make_indicator():
            ca = ColouredArea(self.closed_colour)
            width = opener_settings.indicator_width.get_value_as_int()
            if width:
                ca.set_size_request(width, -1)
                hbox.pack_start(ca, False, False, 0)

            return ca

        self.ca1 = make_indicator()

        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                ""
            ),
            -1,
            "0")[1]

        lvbox = Gtk.VBox()
        hbox.pack_start(lvbox, False, False, 0)

        self._ident_label = Gtk.Label()
        self._ident_label.set_no_show_all(nsa)
        self._ident_label.set_alignment(0.0, 0.0)
        self._ident_label.set_attributes(attrlist)
        lvbox.pack_start(self._ident_label, False, False, 0)

        self._chan_label3 = Gtk.Label()
        self._chan_label3.set_no_show_all(nsa)
        self._chan_label3.set_alignment(0.0, 1.0)
        self._chan_label3.set_attributes(attrlist)
        lvbox.pack_end(self._chan_label3, False, False, 0)

        pad = Gtk.Box()
        hbox.pack_start(pad, True, True, 0)

        self._text_label = Gtk.Label()
        text = opener_tab.button_text.get_text().strip()
        if text:
            self._text_label.set_text(text)
            hbox.pack_start(self._text_label, False, False, 0)

        self._icon_image = Gtk.Image()
        icon = opener_tab.icb.get_filename()
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(icon, 47, 20)
        except (TypeError, GLib.GError):
            pass
        else:
            self._icon_image.set_from_pixbuf(pb)
            hbox.pack_start(self._icon_image, False, False, 0)

        pad = Gtk.Box()
        hbox.pack_start(pad, True, True, 0)

        rvbox = Gtk.VBox()
        hbox.pack_start(rvbox, False, False, 0)

        self._chan_label1 = Gtk.Label()
        self._chan_label1.set_no_show_all(nsa)
        self._chan_label1.set_alignment(1.0, 0.0)
        self._chan_label1.set_attributes(attrlist)
        rvbox.pack_start(self._chan_label1, False, False, 0)

        self._chan_label2 = Gtk.Label()
        self._chan_label2.set_no_show_all(nsa)
        self._chan_label2.set_alignment(1.0, 1.0)
        self._chan_label2.set_attributes(attrlist)
        rvbox.pack_end(self._chan_label2, False, False, 0)

        self.ca2 = make_indicator()

        self.add(hbox)

        to_close = ",".join(str(i) for i, cb in enumerate(
            opener_tab.closer_hbox.get_children(), start=1) if cb.get_active())
        if to_close:
            to_close = "!" + to_close

        self._ident_label.set_text("(%d)%s" % (opener_tab.ident, to_close))

        def labeltext():
            for blk in itertools.zip_longest(*(iter(mic_agc_list),) * 4):
                yield ",".join(x.ui_name for x in blk if x is not None)

        for text, label in zip(
            labeltext(),
                (self._chan_label1, self._chan_label2, self._chan_label3)):
            label.set_text(text)

        self.connect("toggled", self.__cb_toggle)
        self.__flash = False
        self.show_all()


class OpenerTab(Gtk.VBox):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_LAST, None, ())
    }

    def __init__(self, ident):
        super(OpenerTab, self).__init__()
        self.set_border_width(6)
        self.set_spacing(4)
        self.label = Gtk.Label()
        self.label.show()
        self.set_ident(ident)
        self.activedict = {}
        sg = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)
        lhbox = Gtk.Box()
        lhbox.set_spacing(3)
        label = Gtk.Label(label=_('Text'))
        lhbox.pack_start(label, False, False, 0)
        self.button_text = Gtk.Entry()
        set_tip(self.button_text, _("The opener button's text."))
        self.button_text.connect("changed", lambda w: self.emit("changed"))
        sg.add_widget(self.button_text)
        lhbox.pack_start(self.button_text, True, True, 0)

        spc = Gtk.Box()
        lhbox.pack_start(spc, False, False, 2)

        label = Gtk.Label(label=_('Icon'))
        lhbox.pack_start(label, False, False, 0)

        self.icon_chooser = IconPreviewFileChooserDialog(
            "Choose An Icon",
            buttons=(Gtk.STOCK_CLEAR, Gtk.ResponseType.NONE,
                     Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                     Gtk.STOCK_OK, Gtk.ResponseType.OK))
        self.icb = IconChooserButtonExtd(self.icon_chooser)
        set_tip(self.icb, _("The opener button's icon."))
        self.icb.connect("filename-changed", lambda w, r: self.emit("changed"))
        sg.add_widget(self.icb)
        lhbox.pack_start(self.icb, True, False, 0)

        self.pack_start(lhbox, False, False, 0)

        hbox = Gtk.Box()
        set_tip(
            hbox,
            _('The headroom is the amount by which to reduce player '
              'volume when this opener is active. Note that the actual'
              'amount will be the largest value of all '
              'the currently open buttons.'))

        self.pack_start(hbox, False, False, 0)
        label = Gtk.Label(label=_('The amount of headroom required (dB)'))
        label.set_alignment(0.0, 0.5)
        hbox.pack_start(label, False, False, 0)
        self.headroom = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(0.0, 0.0, 32.0, 0.5), digits=1)
        self.headroom.connect("value-changed", lambda w: self.emit("changed"))
        hbox.pack_end(self.headroom, False, False, 0)

        self.has_reminder_flash = Gtk.CheckButton(
            _('This button will flash as a reminder to close'))
        set_tip(
            self.has_reminder_flash,
            _("After a number of seconds where a "
              "main player is active this button's "
              "status indicator will start to "
              "flash and will continue to do so "
              "until the button is closed or the "
              "player stops.")
        )

        self.pack_start(self.has_reminder_flash, False, False, 0)

        self.is_microphone = Gtk.CheckButton(
            _('This button is to be treated as a microphone opener'))

        set_tip(
            self.is_microphone,
            _("The button will be grouped with the "
              "other microphone opener buttons. It "
              "will be affected by signals to "
              "close microphone buttons. Channels "
              "associated with this button will "
              "be mixed differently when using the VoIP modes."))

        self.is_microphone.connect("toggled", lambda w: self.emit("changed"))
        self.pack_start(self.is_microphone, False, False, 0)

        self.freewheel_cancel = Gtk.CheckButton(
            _('This button will automatically cancel JACK freewheel mode'))
        self.pack_start(self.freewheel_cancel, False, False, 0)
        set_tip(self.freewheel_cancel, _('This should be set for all buttons'
                ' that control input from a live sound source or device.'))

        frame = Gtk.Frame(label=" %s " % _('Button Open Triggers'))
        self.pack_start(frame, False, False, 3)
        self.open_triggers = collections.OrderedDict()
        lvbox = Gtk.VBox()
        rvbox = Gtk.VBox()
        for w, t, col in zip(
            ("advance", "stop_control", "stop_control2", "announcement"),
            (_('Playlist advance button'),
             _("'%s' control") % _('Player Stop'),
             _("'%s' control") % _('Player Stop 2'),
             _('Announcements')),
                itertools.cycle((lvbox, rvbox))):
            cb = Gtk.CheckButton(t)
            self.open_triggers[w] = cb
            col.pack_start(cb, False, False, 0)
            self.activedict["oc_" + w] = cb
        hbox = Gtk.Box(True, 10)
        hbox.set_border_width(6)
        for each in (lvbox, rvbox):
            hbox.pack_start(each, False, False, 0)
        frame.add(hbox)

        frame = Gtk.Frame(label=" %s " %
                          _('When opened close these other buttons'))
        self.pack_start(frame, False, False, 3)
        self.closer_hbox = Gtk.Box()
        self.closer_hbox.set_border_width(3)
        for i in range(1, ident):
            cb = Gtk.CheckButton(str(i))
            cb.connect("toggled", lambda w: self.emit("changed"))
            self.closer_hbox.pack_start(cb, True, True, 0)
            self.activedict["close_%d_button" % i] = cb
        frame.add(self.closer_hbox)

        frame = Gtk.Frame(label=" %s " % _('Shell Command'))
        set_tip(frame, _("Mostly useful issuing 'amixer' commands, in "
                         "particular for setting capture."))
        self.pack_start(frame, False, False, 3)
        ivbox = Gtk.VBox()
        frame.add(ivbox)
        ivbox.set_border_width(6)
        ivbox.set_spacing(3)
        sg = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)

        def enbox(l, r):
            hbox = Gtk.Box()
            hbox.set_spacing(3)
            label = Gtk.Label(label=l)
            label.set_alignment(0.0, 0.5)
            hbox.pack_start(label, False, False, 0)
            hbox.pack_start(r, True, True, 0)
            sg.add_widget(r)
            return hbox

        self.shell_on_open = Gtk.Entry()
        self.shell_on_close = Gtk.Entry()
        ivbox.pack_start(enbox(_('On open'), self.shell_on_open),
                         False, False, 0)
        ivbox.pack_start(
            enbox(_('On close'), self.shell_on_close), False, False, 0)

        self.activedict.update({
            "reminderflash": self.has_reminder_flash,
            "isamicrophone": self.is_microphone,
            "cancelsfreewheel": self.freewheel_cancel
        })

        self.valuesdict = {
            "headroom": self.headroom
        }

        self.textdict = {
            "iconpathname": self.icb,
            "buttontext": self.button_text,
            "shell_onopen": self.shell_on_open,
            "shell_onclose": self.shell_on_close,
        }

        self.button_was_on = False

    def set_ident(self, ident):
        self.label.set_text(str(ident))
        self.ident = ident

    def add_closer(self, closer_ident):
        cb = Gtk.CheckButton(str(closer_ident))
        if closer_ident == self.ident:
            cb.set_sensitive(False)
        else:
            cb.connect("toggled", lambda w: self.emit("changed"))
            self.activedict["close_%d_button" % closer_ident] = cb
        self.closer_hbox.pack_start(cb, True, True, 0)
        cb.show()


class OpenerSettings(Gtk.Frame):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None,
                                (GObject.TYPE_PYOBJECT,))}

    def __init__(self):
        super(OpenerSettings, self).__init__()
        self.set_label(" %s " % _('Main Panel Opener Buttons'))
        self.set_border_width(3)

        def changed(*args):
            self.emit("changed", None)

        vbox = Gtk.VBox()
        self.add(vbox)
        vbox.set_border_width(7)
        vbox.set_spacing(3)

        self.button_numbers = Gtk.CheckButton(
            _('Indicate button numbers and associated channel numbers'))

        set_tip(self.button_numbers,
                _("A useful feature to have switched on "
                  "while allocating channel openers."))

        self.button_numbers.connect("toggled", changed)
        vbox.pack_start(self.button_numbers, False, False, 0)

        frame = Gtk.Frame()
        frame.set_label(" %s " % _('Status Indicator Appearance'))

        set_tip(frame,
                _('Each opener button has two vertical '
                  'bars at the side to make the '
                  'button state more apparent. '
                  'These settings control their appearance.'))

        vbox.pack_start(frame, False, False, 6)
        hbox = Gtk.Box()
        hbox.set_border_width(3)
        hbox.set_spacing(3)
        frame.add(hbox)

        hbox.pack_start(Gtk.Label(label=_('Width')), True, True, 0)
        self.indicator_width = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(4.0, 0.0, 10.0, 1.0), digits=0)
        self.indicator_width.connect("value-changed", changed)
        hbox.pack_start(self.indicator_width, False, False, 0)
        hbox.pack_start(Gtk.Box(True, 0), False, False, 0)

        hbox.pack_start(Gtk.Label(_('Opened')), True, True, 0)
        self.open_colour = ColourButton(Gdk.Color(0.95, 0.2, 0.2))
        hbox.pack_start(self.open_colour, False, False, 0)
        hbox.pack_start(Gtk.Box(True, 3), False, False, 0)
        hbox.pack_start(Gtk.Label(_('Closed')), True, True, 0)
        col = Gdk.RGBA(50, 50, 50, 1)
        self.closed_colour = ColourButton(col)
        hbox.pack_start(self.closed_colour, False, False, 0)
        hbox.pack_start(Gtk.Box(True, 3), False, False, 0)
        hbox.pack_start(Gtk.Label(_('Remind')), True, True, 0)
        self.reminder_colour = ColourButton(col)
        hbox.pack_start(self.reminder_colour, False, False, 0)

        for each in (self.open_colour, self.closed_colour,
                     self.reminder_colour):
            each.connect("color-set", changed)

        self.notebook = Gtk.Notebook()
        vbox.pack_start(self.notebook, False, False, 3)
        self.show_all()

        self.activedict = {
            "btnnumbers": self.button_numbers,
        }

        self.textdict = {
            "btncolour_opened": self.open_colour,
            "btncolour_closed": self.closed_colour,
            "btncolour_remind": self.reminder_colour,
        }

        self.valuesdict = {
            "btnreminderwidth": self.indicator_width,
        }

    def add_channel(self):
        tab = OpenerTab(len(self.notebook) + 1)
        self.notebook.append_page(tab, tab.label)

        def add_closer(each_tab):
            each_tab.add_closer(tab.ident)

        self.notebook.foreach(add_closer)
        tab.show_all()
        tab.connect("changed", lambda w: self.emit("changed", tab))

    def finalise(self):
        for tab in self.notebook.get_children():
            for attrname in ("activedict", "valuesdict", "textdict"):
                dest = getattr(self, attrname)
                src = getattr(tab, attrname)
                for key, val in src.items():
                    dest[key + "_%d" % tab.ident] = val


class MicOpener(Gtk.Box):

    @property
    def any_mic_selected(self):
        return self._any_mic_selected

    def notify_others(self, freewheel_cancel=False):
        r = self.approot
        if freewheel_cancel:
            r.freewheel_button.set_active(False)
        # Player headroom for mic-audio toggle.
        r.mixer_write("ACTN=anymic\nFLAG=%d\nend\n" % self.any_mic_selected)
        r.mixer_write("HEAD=%f\nACTN=headroom\nend\n" % self._headroom)
        r.new_mixermode(r.mixermode)

    def cb_mictoggle(self, button, mics):
        self._flashing_timer = 0
        fwc = False

        if button.get_active():
            fwc = button.opener_tab.freewheel_cancel.get_active()
            cmd = button.opener_tab.shell_on_open.get_text().strip()
            closers = button.opener_tab.closer_hbox.get_children()
            for i, closer in enumerate(closers, start=1):
                if closer.get_active():
                    try:
                        self.ix2button[i].set_active(False)
                    except KeyError:
                        pass
        else:
            cmd = button.opener_tab.shell_on_close.get_text().strip()

        if cmd and not button.block_shell_command:
            print("button %d shell command: %s" % (
                button.opener_tab.ident, cmd
            ))
            subprocess.Popen(cmd, shell=True, close_fds=True)

        for mic in mics:
            mic.open.set_active(button.get_active())

        self._any_mic_selected = any(
            mb.get_active() for mb in self.buttons
            if mb.opener_tab.is_microphone.get_active())

        try:
            self._headroom = max(mb.opener_tab.headroom.get_value()
                                 for mb in self.buttons if mb.get_active())
        except ValueError:
            self._headroom = 0.0

        self.notify_others(freewheel_cancel=fwc)

    def cb_reconfigure(self, widget, trigger=None):
        self.new_button_set()

    def new_button_set(self):
        # Clear away old button widgets.
        self.foreach(lambda x: x.destroy())
        self.mic2button = {}
        self.buttons = []
        self.ix2button = {}

        mic_group_list = [[] for x in range(PGlobs.num_micpairs * 2)]
        aux_group_list = [[] for x in range(PGlobs.num_micpairs * 2)]
        ot = self.opener_settings.notebook.get_children()
        mic_qty = aux_qty = 0

        # Categorisation of channels into button groups.
        for m in self.mic_list:
            mode = m.mode.get_active()
            if mode:
                pm = m.partner if mode == 3 else m
                if pm.group.get_active():
                    oti = int(pm.groups_adj.get_value()) - 1
                    if ot[oti].is_microphone.get_active():
                        t = mic_group_list[oti]
                        if not t:
                            mic_qty += 1
                    else:
                        t = aux_group_list[oti]
                        if not t:
                            aux_qty += 1
                    t.append(m)

        # Opener buttons built here.
        def build(group_list, closer):
            image = Gtk.Image.new_from_stock(
                Gtk.STOCK_CLOSE, Gtk.IconSize.BUTTON)
            closer_button = Gtk.Button()
            closer_button.set_image(image)
            closer_button.show_all()

            if closer == "left":
                self.pack_start(closer_button, True)

            for i, g in enumerate(group_list):
                if g:
                    mic_list = []
                    mb = MicButton(self.opener_settings, ot[i], g)
                    self.ix2button[mb.opener_tab.ident] = mb
                    self.buttons.append(mb)
                    active = False
                    for m in g:
                        mic_list.append(m)
                        if m.open.get_active():
                            active = True
                        self.mic2button[m.ui_name] = mb
                    mb.connect("toggled", self.cb_mictoggle, mic_list)
                    self.add(mb)
                    mb.show()
                    mb.block_shell_command = mb.opener_tab.button_was_on
                    mb.set_active(active)
                    mb.block_shell_command = False

                    closer_button.connect(
                        "clicked",
                        lambda w, btn: btn.set_active(False),
                        mb)

            if closer == "right":
                self.pack_start(closer_button, False, False, 0)

        if aux_qty:
            build(aux_group_list, closer=("right" if aux_qty > 1 else None))

        if mic_qty:
            build(mic_group_list, closer=("left" if mic_qty > 1 else None))

        if self._forced_on_mode:
            self.force_all_on(True)

        if not self.mic2button:
            # TC: A placeholder text for when there are no opener buttons.
            l = Gtk.Label(label=_('No Channel Opener Buttons'))
            l.set_sensitive(False)
            self.add(l)
            l.show()

        # Categorisation of channels according to type a or m (aux or mic)
        channel_modes = ['a' for i in range(PGlobs.num_micpairs * 2)]
        for button in mic_group_list:
            for channel in button:
                channel_modes[channel.index] = 'm'

        self.approot.mixer_write("CMOD=%s\nACTN=new_channel_mode_string\nend\n"
                                 % "".join(channel_modes))
        self.notify_others()

    @threadslock
    def cb_flash_timeout(self):

        if self._flash_test() and not self._forced_on_mode:
            self._flashing_timer += 1
        else:
            self._flashing_timer = 0

        flash_value = bool((self._flashing_timer % 2)
                           if self._flashing_timer > 7 else 0)

        for mb in self.buttons:
            mb.flash = flash_value

        return True

    def force_all_on(self, val):
        """Switch on all front panel mic buttons and make them insensitive."""

        self._forced_on_mode = val
        for mb in self.buttons:
            if mb.opener_tab.is_microphone.get_active():
                if val:
                    mb.set_active(True)
                mb.set_sensitive(not val)
                mb.set_inconsistent(val)

    def open_auto(self, type_):
        for b in self.buttons:
            try:
                cb = b.opener_tab.open_triggers[type_]
            except KeyError:
                print("unknown auto open type:", type_)
            else:
                if cb.get_active():
                    b.set_active(True)

    def oc(self, mic, val):
        """Perform open/close."""

        try:
            self.mic2button[mic].set_active(val)
        except:
            for m in self.mic_list:
                if mic == m.ui_name:
                    mode = m.mode.get_active()
                    if mode in (1, 2):
                        m.open.set_active(val)
                    elif mode == 3:
                        m.partner.open.set_active(val)
                    break

    def get_opener_button(self, ix):
        try:
            m = self.mic_list[ix]
            return self.mic2button[m.ui_name]
        except KeyError:
            mode = m.mode.get_active()
            if mode in (1, 2):
                return m.open
            elif mode == 3:
                return m.partner.open
            print("channel %d is not active" % (ix + 1))
        except IndexError:
            print("channel %d does not exist" % (ix + 1))
        return None

    def close_all(self):
        for mb in self.buttons:
            mb.set_active(False)

    def open(self, val):
        self.oc(val, True)

    def close(self, val):
        self.oc(val, False)

    def add_mic(self, mic):
        """mic: AGCControl object passed here to register it with this class."""

        self.opener_settings.add_channel()
        self.mic_list.append(mic)
        for attr, sig in zip(
            ("mode", "group", "no_front_panel_opener", "groups_adj"),
                ("changed", "toggled", "toggled", "notify::value")):
            getattr(mic, attr).connect(sig, self.cb_reconfigure)

    def finalise(self):
        self.opener_settings.finalise()

    def __init__(self, approot, flash_test):
        self.approot = approot
        self._flash_test = flash_test
        super(MicOpener, self).__init__()
        self.set_spacing(2)
        self.set_homogeneous(True)
        self.mic_list = []
        self.buttons = []
        self.mic2button = {}
        self._any_mic_selected = False
        self._forced_on_mode = False
        self._flashing_mode = False
        self._flashing_timer = 0
        self._headroom = 0.0
        timeout = timeout_add(700, self.cb_flash_timeout)
        self.connect("destroy", lambda w: source_remove(timeout))
        self.opener_settings = OpenerSettings()
        self.opener_settings.connect("changed", self.cb_reconfigure)


class PaddedVBox(Gtk.VBox):

    def vbox_pack_start(self, *args, **kwargs):
        self.vbox.pack_start(*args, **kwargs)

    def vbox_add(self, *args, **kwargs):
        self.vbox.add(*args, **kwargs)

    def __init__(self, l, t, r, b, s):
        super(PaddedVBox, self).__init__()
        d = Gtk.VBox()
        self.pack_start(d, False, False, t)
        d.show()
        d = Gtk.VBox()
        self.pack_end(d, False, False, b)
        d.show()
        h = Gtk.Box()
        self.pack_start(h, True, True, 0)
        h.show()
        d = Gtk.VBox()
        h.pack_start(d, False, False, l)
        d.show()
        d = Gtk.VBox()
        h.pack_end(d, False, False, r)
        d.show()
        self.vbox = Gtk.VBox()
        self.vbox.set_spacing(s)
        h.pack_start(self.vbox, True, True, 0)
        self.vbox.show()
        self.pack_start = self.vbox_pack_start
        self.add = self.vbox_add


def make_meter_scale():         # A logarithmic meter scale for a 'VU' meter
    scalebox = Gtk.VBox()

    lbls = collections.OrderedDict([
        ("  0", [0, 0, 1, 1]),
        (" -6", [0, 0, 1, 1]),
        ("-12", [0, 0.25, 1, 1]),
        ("-18", [0, 0.5, 1, 1]),
        ("-24", [0, 0.75, 1, 1]),
        ("-30", [0, 1, 1, 1]),
        ("-36", [0, 1, 1, 1])
    ])

    for lbl, align in lbls.items():
        label = Gtk.Label(label=lbl)
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                lbl
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        alignment = Gtk.Alignment.new(*align)
        alignment.add(label)
        label.show()
        scalebox.add(alignment)
        alignment.show()

    return scalebox


def make_meter_unit(text, l_meter, r_meter):
    mic_peak_box = Gtk.VBox()
    mic_peak_box.set_border_width(0)
    frame = Gtk.Frame()
    frame.set_border_width(4)
    hbox = Gtk.Box()
    hbox.set_border_width(1)
    frame.add(hbox)
    label = Gtk.Label(label=text)
    attrlist = Pango.parse_markup(
        '<span size="{}">{}</span>'.format(
            METER_TEXT_SIZE,
            text
        ),
        -1,
        "0")[1]
    label.set_attributes(attrlist)
    labelbox = Gtk.Box()
    labelbox.add(label)
    label.show()
    mic_peak_box.pack_start(labelbox, False, False, 0)
    labelbox.show()
    mic_peak_box.add(frame)
    frame.show()
    hbox.show()
    l_meter.set_size_request(16, -1)
    hbox.add(l_meter)
    scalebox = make_meter_scale()
    hbox.pack_start(scalebox, True, True, 0)
    scalebox.show()
    r_meter.set_size_request(16, -1)
    hbox.add(r_meter)
    l_meter.show()
    r_meter.show()
    return mic_peak_box


def make_stream_meter_unit(text, meters):
    outer_vbox = Gtk.VBox()
    outer_vbox.set_border_width(0)
    frame = Gtk.Frame()
    frame.set_border_width(4)
    inner_vbox = Gtk.VBox()
    frame.add(inner_vbox)
    label = Gtk.Label(label=text)
    attrlist = Pango.parse_markup(
        '<span size="{}">{}</span>'.format(
            METER_TEXT_SIZE,
            text
        ),
        -1,
        "0")[1]
    label.set_attributes(attrlist)
    labelbox = Gtk.Box()
    labelbox.add(label)
    label.show()
    outer_vbox.pack_start(labelbox, False, False, 0)
    labelbox.show()
    outer_vbox.pack_start(frame, False, False, 0)
    frame.show()
    inner_vbox.show()
    for num, meter in enumerate(meters):
        hbox = Gtk.Box()
        hbox.set_border_width(1)
        hbox.set_spacing(1)
        inner_vbox.add(hbox)
        hbox.show()
        label = Gtk.Label(label=str(num + 1))
        hbox.pack_start(label, False, False, 0)
        label.show()
        vbox = Gtk.VBox()
        vbox.pack_start(meter, True, True, 2)
        meter.show()
        hbox.pack_start(vbox, True, True, 0)
        vbox.show()
    set_tip(
        frame,
        _('This indicates the state of the various streams. Flashing'
          ' means stream packets are being discarded because of network '
          'congestion. Partial red means the send buffer is partially '
          'full indicating difficulty communicating with the server. '
          'Green means everything is okay.'))

    frame = Gtk.Frame()  # Main panel listener figures box.
    frame.set_label_align(0.5, 0.5)
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
        FGlobs.pkgdatadir / "listenerphones.png", 20, 16)
    image = Gtk.Image.new_from_pixbuf(pixbuf)
    frame.set_label_widget(image)
    image.show()

    frame.set_border_width(4)
    inner_vbox = Gtk.VBox()
    frame.add(inner_vbox)
    inner_vbox.show()
    connections = Gtk.Label(label="0")
    inner_vbox.add(connections)
    connections.show()
    outer_vbox.pack_start(frame, False, False, 0)
    frame.show()
    set_tip(frame,
            _('The combined total number of listeners in all server tabs.'))

    return outer_vbox, connections


class StreamMeter(Gtk.Frame):

    """Main panel meter showing stream status and buffer fill."""

    def realize(self, widget):
        self.green = Gdk.RGBA()
        self.red = Gdk.RGBA()
        self.grey = Gdk.RGBA()

        self.green.parse("#30D030")
        self.red.parse("#D05044")
        self.grey.parse("darkgray")

    def expose(self, widget, event):
        if not self.da.get_realized():
            return

        ctx = Gdk.cairo_create(self.da.get_window())
        if self.flash or not self.active:
            Gdk.cairo_set_source_rgba(ctx, self.grey)
            ctx.rectangle(0, 0, self.rect.width, self.rect.height)
            ctx.fill()
        else:
            valuep = int(float(self.value - self.base) /
                         float(self.top - self.base) * self.rect.width)
            Gdk.cairo_set_source_rgba(ctx, self.red)
            ctx.rectangle(0, 0, valuep, self.rect.height)
            ctx.fill()
            Gdk.cairo_set_source_rgba(ctx, self.green)
            ctx.rectangle(valuep, 0,
                          self.rect.width - valuep, self.rect.height)
            ctx.fill()

    def cb_configure(self, widget, event):
        if isinstance(event, Gdk.EventConfigure):
            #self.rect = (event.width, event.height)
            self.rect.width = event.width
            self.rect.height = event.height

    def set_value(self, value):
        if value < self.base:
            self.value = self.base
        elif self.value > self.top:
            self.value = self.top
        else:
            self.value = value
        if value != self.oldvalue:
            self.invalidate()

    def set_active(self, active):
        if active != self.active:
            self.active = active
            self.invalidate()

    def set_flash(self, flash):
        if flash != self.flash:
            self.flash = flash
            self.invalidate()

    def invalidate(self):
        if self.da.get_realized() and self.da.get_window():
            self.da.get_window().invalidate_rect(self.rect, False)

    def __init__(self, base, top):
        self.base = base
        self.top = top
        self.gc = None
        super(StreamMeter, self).__init__()
        self.set_shadow_type(Gtk.ShadowType.IN)
        self.da = Gtk.DrawingArea()
        self.add(self.da)
        self.da.connect("configure_event", self.cb_configure)
        self.da.connect("realize", self.realize)
        self.da.connect("draw", self.expose)
        self.da.show()
        self.rect = Gdk.Rectangle()
        self.value = self.oldvalue = self.base
        self.active = False
        self.flash = False


class BasicMeter(Gtk.Frame):

    """A meter widget with a simple rectangular vertical bar."""

    def realize(self, widget):
        self.lowc = Gdk.RGBA()
        self.midc = Gdk.RGBA()
        self.highc = Gdk.RGBA()
        self.backc = Gdk.RGBA()
        self.linec = Gdk.RGBA()

        self.lowc.parse("#30D030")
        self.midc.parse("#CCCF44")
        self.highc.parse("#D05044")
        self.backc.parse("darkgray")
        self.linec.parse("#505050")

    def expose(self, widget, event):
        self.oldvalue = self.top
        self.set_value(self.value)
        if self.value != self.base:
            self.oldvalue = self.base
            self.set_value(self.value)

    def cb_configure(self, widget, event):
        self.width = event.width
        self.height = event.height
        # calculate colour threshold pixels
        self.lutp = int(self.height * float(self.lut - self.base) /
                        float(self.top - self.base))
        self.mutp = int(self.height * float(self.mut - self.base) /
                        float(self.top - self.base))

    def set_value(self, value):
        if value > self.top:
            value = self.top
        if value < self.base:
            value = self.base
        self.value = value
        if self.da.get_realized():
            ctx = Gdk.cairo_create(self.da.get_window())
            valuep = int(self.height * float(self.value - self.base) /
                         float(self.top - self.base))
            Gtk.render_background(self.da.get_style_context(), ctx,
                                  0, 0, self.width, self.height)
            if value < self.oldvalue:
                Gdk.cairo_set_source_rgba(ctx, self.backc)
                ctx.rectangle(0, 0, self.width, self.height - valuep)
                ctx.fill()
            if value > self.oldvalue:
                if valuep > self.mutp:
                    Gdk.cairo_set_source_rgba(ctx, self.highc)
                    ctx.rectangle(
                        0,
                        self.height - valuep,
                        self.width,
                        valuep - self.mutp)
                    ctx.fill()
                    valuep = self.mutp
                if valuep > self.lutp:
                    Gdk.cairo_set_source_rgba(ctx, self.midc)
                    ctx.rectangle(
                        0,
                        self.height - valuep,
                        self.width,
                        valuep - self.lutp)
                    ctx.fill()
                    valuep = self.lutp
                if valuep > 0:
                    Gdk.cairo_set_source_rgba(ctx, self.lowc)
                    ctx.rectangle(0, self.height - valuep, self.width, valuep)
                    ctx.fill()
            if self.line is not None:
                valuel = int(self.height * float(self.line - self.base) /
                             float(self.top - self.base))
                Gdk.cairo_set_source_rgba(ctx, self.linec)
                ctx.move_to(0, self.height - valuel)
                ctx.line_to(self.width, self.height - valuel)
                ctx.stroke()
            self.oldvalue = value

    def set_line(self, lineval):
        if lineval is not None and (
                lineval >= self.top or lineval <= self.base):
            lineval = None
        self.line = lineval
        self.expose(None, None)

    def get_value(self):
        return self.value

    def __init__(self, base, top, lut, mut):
        """This widget will draw in up to three colours.

        mut = mid upper threshold, lut = low upper threshold
        """

        assert top > base, "top must be greater than base"
        assert lut >= base, "lut must be greater than or equal to base"
        assert lut <= top, "lut must not exceed top"
        assert mut >= lut, "mut must be greater than or equal to lut"
        assert mut <= top, "mut must not exceed top"
        super(BasicMeter, self).__init__()
        self.set_shadow_type(Gtk.ShadowType.IN)
        self.da = Gtk.DrawingArea()
        self.add(self.da)
        self.da.connect("configure_event", self.cb_configure)
        self.da.connect("realize", self.realize)
        self.da.connect("draw", self.expose)
        self.da.show()
        self.base = base
        self.top = top
        self.lut = lut
        self.mut = mut
        self.value = base
        self.oldvalue = self.top
        self.line = None


class StackedMeter(Gtk.Frame):

    """Meter with three fill levels showing as different colours."""

    def realize(self, widget):
        self.ngc = Gdk.RGBA()
        self.dsc = Gdk.RGBA()
        self.compc = Gdk.RGBA()
        self.backc = Gdk.RGBA()

        self.ngc.parse("#30D030")
        self.dsc.parse("#CCCF44")
        self.compc.parse("#D05044")
        self.backc.parse("darkgray")

    def expose(self, widget, event):
        self.set_meter_value(self.c, self.d, self.n, True)

    def cb_configure(self, widget, event):
        self.width = event.width
        self.height = event.height
        self.uh = self.height / float(self.top - self.base)

    def set_meter_value(self, c, d, n, force=False):
        if not force and (self.c == c and self.d == d and self.n == n):
            # Values not changed from last time so no need to redraw.
            return
        if c < self.base:
            c = self.base
        if d < self.base:
            d = self.base
        if n < self.base:
            n = self.base
        if c > self.top:
            c = self.top
        if d > self.top:
            d = self.top
        if n > self.top:
            n = self.top
        self.c = c
        self.d = d
        self.n = n
        if self.da.get_realized():
            ctx = Gdk.cairo_create(self.da.get_window())
            nh = int(self.uh * n)
            dh = int(self.uh * d)
            ch = int(self.uh * c)
            if ch + dh + nh > self.height:
                ch = self.height - dh - nh
            if nh:
                ctx.set_source_rgb(self.ngc.red, self.ngc.green, self.ngc.blue)
                ctx.rectangle(0, 0, self.width, nh)
                ctx.fill()
            if dh:
                ctx.set_source_rgb(self.dsc.red, self.dsc.green, self.dsc.blue)
                ctx.rectangle(0, nh, self.width, dh)
                ctx.fill()
            if ch:
                ctx.set_source_rgb(
                    self.compc.red, self.compc.green, self.compc.blue)
                ctx.rectangle(0, nh + dh, self.width, ch)
                ctx.fill()

            ctx.set_source_rgb(
                self.backc.red, self.backc.green, self.backc.blue)
            ctx.rectangle(0, nh + dh + ch, self.width,
                          self.height - (nh + dh + ch))
            ctx.fill()

    def __init__(self, base, top):
        self.base = base
        self.top = top
        assert top > base, "top must be greater than base"
        super(StackedMeter, self).__init__()
        self.set_shadow_type(Gtk.ShadowType.IN)
        self.da = Gtk.DrawingArea()
        self.add(self.da)
        self.da.connect("configure_event", self.cb_configure)
        self.da.connect("realize", self.realize)
        self.da.connect("draw", self.expose)
        self.da.show()
        self.c = self.d = self.n = base - 1


class vumeter(BasicMeter):

    """A VU meter that needs to be fed values at 50ms intervals."""

    def set_meter_value(self, newvalue):
        if newvalue > self.scale:
            newvalue = self.scale

        self.gen6 = self.gen5
        self.gen5 = self.gen4
        self.gen4 = self.gen3
        self.gen3 = self.gen2
        self.gen2 = self.gen1
        self.gen1 = newvalue

        # Weighted mean over 300ms.
        newvalue = (5 * self.gen1 + 6 * self.gen2 + 4 * self.gen3 +
                    3 * self.gen4 + 2 * self.gen5 + self.gen6) / 21
        BasicMeter.set_value(self, -newvalue)

    def __init__(self):
        BasicMeter.__init__(self, -36, 0, -12, -7)
        self.scale = 36
        self.gen1 = self.gen2 = self.gen3 = self.gen4 = self.gen5 = self.scale


class peakholdmeter(BasicMeter):

    """A peak-hold meter."""

    def set_meter_value(self, newval):
        oldval = self.get_value()
        if newval > oldval:
            self.peakage = 0
            oldval = newval
        else:
            self.peakage += 1
            if self.peakage > self.peakholditers:
                newval = oldval - (self.peakage - self.peakholditers) ** 1.1
            else:
                newval = oldval
        BasicMeter.set_value(self, newval)

    def __init__(self):
        BasicMeter.__init__(self, -36, 0, -12, -2)
        self.peakage = 0
        self.oldval = 0
        self.peakholditers = 4  # Meter hold iterations.


class MicMeter(Gtk.VBox):

    def set_meter_value(self, newvals):
        gain, red, yellow, green = (int(x) for x in newvals.split(","))
        self.peak.set_meter_value(gain)
        self.attenuation.set_meter_value(red, yellow, green)

    def set_led(self, value):
        self.led.set_from_pixbuf(self.led_onpb if value else self.led_offpb)

    def always_show(self, widget):
        self.show_while_inactive = widget.get_active()
        if self.show_while_inactive:
            self.show()
        elif not (self.is_sensitive()):
            self.hide()

    def set_sensitive(self, value):
        Gtk.VBox.set_sensitive(self, value)
        if self.show_while_inactive is False and value is False:
            self.hide()
        else:
            self.show()

    def _cb_tooltip(self, widget, x, y, keyboard_mode, tooltip):
        if self.agc:
            text = self.agc.alt_name.get_text().strip()
            if not text:
                return False
            label = Gtk.Label(label=text)
            tooltip.set_custom(label)
            label.show()
            return True
        else:
            return False

    def __init__(self, labelbasetext, index):
        super(MicMeter, self).__init__()
        self.set_border_width(0)
        lhbox = Gtk.Box()
        pad = Gtk.VBox()
        lhbox.add(pad)
        pad.show()
        lhbox.set_spacing(2)
        self.led_onpb = GdkPixbuf.Pixbuf.new_from_file_at_size(
            FGlobs.pkgdatadir / "led_lit_green_black_border_64x64.png", 7, 7)
        self.led_offpb = GdkPixbuf.Pixbuf.new_from_file_at_size(
            FGlobs.pkgdatadir / "led_unlit_clear_border_64x64.png", 7, 7)
        self.led = Gtk.Image()
        lhbox.pack_start(self.led, False, False, 0)
        self.set_led(False)
        self.led.show()
        labeltext = labelbasetext + " " + str(index)
        label = Gtk.Label(label=labeltext)
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                labeltext
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        lhbox.pack_start(label, False, False, 0)
        label.show()
        pad = Gtk.VBox()
        lhbox.add(pad)
        pad.show()
        self.pack_start(lhbox, False, False, 0)
        lhbox.show()
        frame = Gtk.Frame()
        frame.set_border_width(4)
        self.pack_start(frame, True, True, 0)
        frame.show()
        hbox = Gtk.Box()
        hbox.set_border_width(1)
        frame.add(hbox)
        hbox.show()

        self.peak = peakholdmeter()
        self.peak.set_size_request(16, -1)
        hbox.pack_start(self.peak, False, False, 0)
        self.peak.show()

        scale = make_meter_scale()
        hbox.pack_start(scale, False, False, 0)
        scale.show()

        self.attenuation = StackedMeter(0, 36)
        self.attenuation.set_size_request(16, -1)
        hbox.pack_start(self.attenuation, False, False, 0)
        self.attenuation.show()
        self.show_while_inactive = True
        self.agc = None
        self.set_tooltip_window(None)
        self.connect("query-tooltip", self._cb_tooltip)
        self.set_has_tooltip(True)


class RecIndicator(Gtk.Box):
    colour = "clear", "red", "amber"

    def set_indicator(self, colour):
        self.image.set_from_pixbuf(self.led[self.colour.index(colour)])

    def __init__(self, label_text):
        super(RecIndicator, self).__init__()
        label = Gtk.Label(label=label_text)
        self.pack_start(label, True, True, 0)
        label.show()
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                ""
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        self.image = Gtk.Image()
        self.pack_start(self.image, False, False, 0)
        self.image.show()

        self.led = [GdkPixbuf.Pixbuf.new_from_file_at_size(
            FGlobs.pkgdatadir / (which + ".png"), 9, 9) for which in (
            "led_unlit_clear_border_64x64", "led_lit_red_black_border_64x64",
            "led_lit_amber_black_border_64x64")]
        self.set_indicator("clear")


class RecordingPanel(Gtk.VBox):

    def __init__(self, howmany):
        super(RecordingPanel, self).__init__()

        # TC: Record as in, to make a recording.
        label = Gtk.Label(label=" %s " % _('Record'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Record')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        self.pack_start(label, True, True, 0)
        label.show()
        frame = Gtk.Frame()
        frame.set_border_width(4)
        self.pack_start(frame, True, True, 0)
        frame.show()
        hbox = Gtk.Box()
        hbox.set_spacing(1)
        hbox.set_border_width(3)
        frame.add(hbox)
        hbox.show()
        box = [Gtk.VBox(), Gtk.VBox()]
        for each in box:
            each.set_spacing(4)
            hbox.pack_start(each, True, True, 0)
            each.show()
        self.indicator = []
        for i in range(howmany):
            ind = RecIndicator(str(i + 1))
            self.indicator.append(ind)
            box[i % 2].pack_start(ind, False, False, 0)
            ind.show()


# A dialog window to appear when shutdown is selected while still streaming.
class idjc_shutdown_dialog:

    def window_attn(self, widget, event):
        if event.new_window_state | Gdk.WindowState.ICONIFIED:
            widget.set_urgency_hint(True)
        else:
            widget.set_urgency_hint(False)

    def respond(self, dialog, response, actionyes, actionno):
        if response == Gtk.ResponseType.OK:
            print("Dialog quit")
            if actionyes is not None:
                actionyes()
        if response == Gtk.ResponseType.DELETE_EVENT or \
                response == Gtk.ResponseType.CANCEL:
            print("Dialog keep running")
            if actionno is not None:
                actionno()
        dialog.destroy()

    def __init__(self, window_group=None, actionyes=None, actionno=None,
                 additional_text=None):
        dialog = Gtk.Dialog(
            pm.title_extra.strip(),
            None,
            Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            (Gtk.STOCK_CANCEL,
             Gtk.ResponseType.CANCEL,
             Gtk.STOCK_QUIT,
             Gtk.ResponseType.OK)
        )
        if window_group is not None:
            window_group.add_window(dialog)
        dialog.set_resizable(False)
        dialog.connect("close", self.respond, actionyes, actionno)
        dialog.connect("response", self.respond, actionyes, actionno)
        dialog.connect("window-state-event", self.window_attn)
        dialog.set_border_width(6)
        dialog.vbox.set_spacing(12)

        hbox = Gtk.Box(False, 20)
        hbox.set_spacing(12)
        dialog.get_content_area().add(hbox)
        image = Gtk.Image.new_from_stock(
            Gtk.STOCK_DIALOG_WARNING, Gtk.IconSize.DIALOG)
        image.set_alignment(0.5, 0)
        hbox.pack_start(image, False, False, 0)

        vbox = Gtk.VBox()
        hbox.pack_start(vbox, True, True, 0)
        vbox.show()

        if additional_text is not None:
            if type(additional_text) is str:
                additional_text = additional_text.splitlines()
            for each in additional_text:
                label = Gtk.Label()
                label.set_alignment(0.0, 0.5)
                label.set_markup(each)
                vbox.pack_start(label, False, False, 0)
        dialog.show_all()


class MainWindow(dbus.service.Object):

    def send_new_mixer_stats(self):

        deckadj = deck2adj = self.deckadj.get_value()
        if self.prefs_window.dual_volume.get_active():
            deck2adj = self.deck2adj.get_value()

        string_to_send = ":%03d:%03d:%03d:%03d:%03d:%03d:%03d:%03d:%03d:" \
            "%d:%d%d%d%d%d:%d%d:%d%d%d%d:%d:%d:%d:%d:%d:%f:%f:" \
            "%d:%f:%d:%d:%d:%d:%d:%d:%d:%03d:%f:" % (
                deckadj,
                deck2adj,
                self.crossadj.get_value(),
                self.jingles.jvol_adj[0].get_value(),
                self.jingles.jmute_adj[0].get_value(),
                self.jingles.jvol_adj[1].get_value(),
                self.jingles.jmute_adj[1].get_value(),
                self.jingles.ivol_adj.get_value(),
                self.mixbackadj.get_value(),
                self.jingles.playing,
                self.player_left.stream.get_active(),
                self.player_left.listen.get_active(),
                self.player_right.stream.get_active(),
                self.player_right.listen.get_active(),
                self.listen_stream.get_active(),
                self.player_left.pause.get_active(),
                self.player_right.pause.get_active(),
                self.player_left.flush,
                self.player_right.flush,
                self.jingles.flush,
                self.jingles.interludeflush,
                self.simplemixer,
                self.alarm,
                self.mixermode,
                True,
                self.player_left.play.get_active() or
                self.player_right.play.get_active(),
                1.0 / self.player_left.pbspeedfactor,
                1.0 / self.player_right.pbspeedfactor,
                self.prefs_window.speed_variance.get_active(),
                self.prefs_window.dj_aud_adj.get_value(),
                self.crosspattern.get_active(),
                self.dsp_button.get_active(),
                self.jingles.interlude.pause.get_active(),
                self.jingles.interlude.stream.get_active(),
                self.jingles.interlude.listen.get_active(),
                self.jingles.interlude.force.get_active(),
                self.prefs_window.alarm_aud_adj.get_value(),
                self.voipgainadj.get_value(),
                1.0 / self.jingles.interlude.pbspeedfactor
            )
        self.mixer_write("MIXR=%s\nACTN=mixstats\nend\n" % string_to_send)

        self.alarm = False
        while self.player_left.flush or self.player_right.flush or \
                self.jingles.flush or self.jingles.interludeflush:
            time.sleep(0.05)
            self.vu_update(False)
            self.jingles.interludeflush = self.jingles.interludeflush & \
                self.interlude_playing.value
            self.jingles.flush = self.jingles.flush & \
                self.jingles_playing.value
            self.player_left.flush = self.player_left.flush & \
                self.player_left.mixer_playing.value
            self.player_right.flush = self.player_right.flush & \
                self.player_right.mixer_playing.value

        # decide which metadata source to use (0 = left, 1 = right)
        if self.metadata_src == self.METADATA_LEFT_DECK:
            meta = 0
        elif self.metadata_src == self.METADATA_RIGHT_DECK:
            meta = 1
        elif self.metadata_src == self.METADATA_LAST_PLAYED:
            if self.last_player == "left":
                meta = 0
            else:
                meta = 1
        elif self.metadata_src == self.METADATA_CROSSFADER:
            if self.crossadj.get_value() < 50:
                meta = 0
            else:
                meta = 1
        elif self.metadata_src == self.METADATA_NONE:
            meta = -1
        elif self.metadata_src == self.METADATA_BACKGROUND:
            meta = 2

        # get metadata from left (meta == 0) or right (meta == 1) player
        target = (self.player_left, self.player_right,
                  self.jingles.interlude, None)[meta]
        meta_context = None
        if target is None:
            self.songname = self.artist = self.title = self.album = ""
            self.music_filename = ""
        else:
            if target.element:
                self.artist = target.cuesheet_track_performer or ""
                self.title = target.cuesheet_track_title or ""
                self.album = target.cuesheet_track_album or target.title
            else:
                self.artist = target.artist
                self.title = target.title
                self.album = target.album

            self.songname = target.songname
            self.music_filename = target.music_filename
            meta_context = target, target.player_cid, \
                self.artist, self.title, self.album, self.music_filename

        # update metadata on stream if it has changed
        if meta_context != self.old_meta_context:
            self.old_meta_context = meta_context
            if self.songname:
                if target.element:
                    if "(" in self.album or ")" in self.album:
                        form = "%s - %s - [%s]"
                    else:
                        form = "%s - %s - (%s)"
                    self.songname = form % (
                        self.artist,
                        self.title,
                        self.album
                    )

                self.set_track_metadata(
                    self.artist,
                    self.title,
                    self.album,
                    self.songname,
                    self.music_filename,
                    True
                )
            else:
                self.window.set_title(self.appname + pm.title_extra)

            print("song title: %s\n" % self.songname)

    @dbus.service.method(dbus_interface=PGlobs.dbus_bus_basename)
    def new_plugin_started(self):
        self.channel_states = [-1, ] * 12
        self.dbus_voip_mode = -1

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename, signature="u")
    def voip_mode_changed(self, mode):
        pass

    @dbus.service.method(dbus_interface=PGlobs.dbus_bus_basename,
                         out_signature="s")
    def get_database_credentials(self):
        return json.dumps(self.topleftpane.prefs_controls.credentials())

    @dbus.service.method(dbus_interface=PGlobs.dbus_bus_basename,
                         in_signature="sssssb")
    def set_track_metadata(self, artist, title, album, songname, filename, log):
        args = artist, title, album, songname, filename

        self.window.set_title("%s :: IDJC%s" % (songname,
                                                pm.title_extra))
        if log:
            tm = time.localtime()
            ts = "%02d:%02d :: " % (tm[3], tm[4])  # hours and minutes
            tstext = str(songname)
            self.history_buffer.place_cursor(
                self.history_buffer.get_end_iter()
            )
            self.history_buffer.insert_at_cursor(ts + tstext + "\n")
            adjustment = self.history_window.get_vadjustment()
            adjustment.set_value(adjustment.get_upper())
            try:
                with open(pm.basedir / "history.log", "a") as f:
                    f.write(time.strftime("%x %X :: ") + tstext + "\n")
            except IOError:
                print("failed to write log entry to history.log")

        if self._old_metadata_2 == args:
            return

        self._old_metadata_2 = args
        self.track_metadata_changed(*args)
        # Don't pass music_filename
        self.server_window.new_metadata(*args[:-1])

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename,
                         signature="sssss")
    def track_metadata_changed(self, artist, title, album, songname,
                               music_filename):
        """DBus signal for plugins to attach to for metadata updates."""

        print("track_metadata_changed called and signal emitted")

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename,
                         signature="ssu")
    def effect_started(self, title, pathname, player):
        """DBus signal for plugins to attach to when new effects play"""
        pass

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename,
                         signature="u")
    def effect_stopped(self, player):
        """DBus signal for plugins to attach to when new effects play"""
        pass

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename,
                         signature="ub")
    def channelstate_changed(self, index, is_open):
        """DBus signal indicating audio channel toggle state"""
        self.channel_states[index] = is_open

    def songname_decode(self, data):
        i = 1
        while 1:
            if data[i - 1] != "d":
                print("songname_decode: WARNING, read past end boundary")
                yield None
                continue
            colon_index = data.index(":", i)
            text_length = int(data[i: colon_index])
            text = data[colon_index + 1: colon_index + 1 + text_length]
            yield text
            i = colon_index + text_length + 2

    def update_songname(self, player, data):
        gen = self.songname_decode(data)
        infotype = int(next(gen))
        artist = next(gen)
        title = next(gen)
        album = next(gen)
        player_context = int(next(gen))
        time_lag = int(next(gen))

        # if infotype in (1, 2):
        #    artist = artist.decode("utf-8")
        #    title = title.decode("utf-8")
        #    album = album.decode("utf-8")
        # infotype = 1  # Chain

        # if infotype in (3, 4):
        #    artist = artist.decode("latin1")
        #    title = title.decode("latin1")
        #    album = album.decode("latin1")
        # infotype = 1  # Chain

        if infotype == 1:
            def fmt(artist, title, album):
                o, c = ("[", "]") if "(" in album or ")" in album else (
                    "(", ")")
                return "%s - %s - %s%s%s" % (artist, title, o, album, c)

            if not album and not artist:
                sep = title.count(" - ")
                if sep == 2:
                    artist, title, album = title.split(" - ")
                elif sep == 1:
                    artist, title = title.split(" - ")

                if artist and title and album:
                    song = fmt(artist, title, album)
                elif artist and title:
                    song = " - ".join((artist, title))
                else:
                    song = title
            elif not album:
                song = " - ".join((artist, title))
            else:
                song = fmt(artist, title, album)

            #artist = artist.encode("utf-8")
            #title = title.encode("utf-8")
            #album = album.encode("utf-8")

        if infotype == 7:
            model = player.model_playing
            iter = player.iter_playing

            song = model.get_value(iter, 3)
            artist = model.get_value(iter, 6)
            title = model.get_value(iter, 5)
            album = model.get_value(iter, 9)
        # unicode chapter tags unsupported
        if infotype > 4 and infotype < 7:
            return
        if not player_context & 1:
            time_lag = 0
        else:
            time_lag = int(time_lag / player.pbspeedfactor)
        timeout_add(time_lag, self.new_songname_timeout,
                    (song, artist, title, album, player, player_context))

    @threadslock
    def new_songname_timeout(self, xxx_todo_changeme):
        (
            song,
            artist,
            title,
            album,
            player,
            player_context
        ) = xxx_todo_changeme

        if player.player_cid == (player_context | 1) and \
                player.cuesheet is None:
            player.songname = song
            player.artist = artist
            player.title = title
            player.album = album
            self.send_new_mixer_stats()
        else:
            print("context mismatch, player context id =", player.player_cid,
                  "metadata update carries context id =", player_context)
        return False

    def ui_detail_leveller(self, level):
        def inner(widget):
            try:
                widget.forall(inner)
            except AttributeError:
                pass
            try:
                l = widget.viewlevels
            except AttributeError:
                pass
            else:
                if level in l:
                    widget.show()
                else:
                    widget.hide()
        return inner

    def callback(self, widget, data):
        print("%s was pressed" % data)
        if data == "Show about":
            self.prefs_window.notebook.set_current_page(4)
            self.prefs_window.window.present()
        if data == "Features":
            if widget.get_active():
                self.simplemixer = False
                self.min_wst.set_tracking(False)
                self.window.forall(self.ui_detail_leveller(5))
                self.send_new_mixer_stats()
                for each in (self.player_left, self.player_right):
                    each.pl_mode.emit("changed")
                self.full_wst.apply()
                self.full_wst.set_tracking(True)
            else:
                self.simplemixer = True
                self.full_wst.set_tracking(False)
                self.player_right.stop.clicked()
                self.crossadj.set_value(0)
                self.crossadj.value_changed()
                self.window.forall(self.ui_detail_leveller(0))
                for each in (self.player_left, self.player_right):
                    each.pl_delay.set_sensitive(False)
                self.min_wst.apply()
                self.min_wst.set_tracking(True)
        if data == "Advance":
            if self.crossfade.get_value() < 50:
                self.player_left.advance()
            else:
                self.player_right.advance()
        if data.startswith("cfm"):
            if self.crosspass:
                source_remove(self.crosspass)
                self.crosspass = 0
            self.crossfade.set_value(data == "cfmright" and 100
                                     or data == "cfmmidl" and 48
                                     or data == "cfmmidr" and 52
                                     or data == "cfmleft" and 0)
        if data == "pass-crossfader":
            if self.crosspass:
                self.crossdirection = not self.crossdirection
            else:
                self.crossdirection = (self.crossadj.get_value() <= 50)
                self.crosspass = timeout_add(
                    int(self.passspeed_adj.get_value() * 10),
                    self.cb_crosspass)
        if data == "Clear History":
            self.history_buffer.set_text("")

    def expandercallback(self, expander, param_spec, user_data=None):
        if expander.get_expanded():
            self.history_vbox.show()
        else:
            self.history_vbox.hide()
        if self.player_left.is_playing:
            self.player_left.reselect_cursor_please = True
        if self.player_right.is_playing:
            self.player_right.reselect_cursor_please = True

    @threadslock
    def cb_crosspass(self):
        x = self.crossadj.get_value()
        if x == 100 * self.crossdirection:
            self.crosspass = 0
            return False
        if self.crossdirection:
            self.crossfade.set_value(x + 1)
        else:
            self.crossfade.set_value(x - 1)
        return True

    # handles selection of metadata source
    def cb_metadata_source(self, widget):
        print("Metadata source was changed. Before: %d" % self.metadata_src)
        self.metadata_src = widget.get_active()
        print("Metadata source was changed. Now: %d" % self.metadata_src)

        for each in (self.player_left, self.player_right,
                     self.jingles.interlude):
            each.expire_metadata()

        # update mixer status and metadata
        self.send_new_mixer_stats()
        return True

    def cb_toggle(self, widget, data):

        print("%s was toggled %s" % (
            data, ("OFF", "ON")[widget.get_active()]))
        if data == "stream-mon":
            self.send_new_mixer_stats()

        if data == "Greenphone":
            mode = self.mixermode
            if widget.get_active() is True:
                if self.mixermode == self.PRIVATE_PHONE:
                    self.mixermode = self.PUBLIC_PHONE
                    self.redphone.set_active(False)
                self.mixermode = self.PUBLIC_PHONE
            else:
                if self.mixermode == self.PUBLIC_PHONE:
                    self.mixermode = self.NO_PHONE
            if self.mixermode != mode:
                self.new_mixermode(self.mixermode)

        if data == "Redphone":
            mode = self.mixermode
            if widget.get_active() is True:
                if self.mixermode == self.PUBLIC_PHONE:
                    self.mixermode = self.PRIVATE_PHONE
                    self.greenphone.set_active(False)
                self.mixermode = self.PRIVATE_PHONE
            else:
                if self.mixermode == self.PRIVATE_PHONE:
                    self.mixermode = self.NO_PHONE
            if self.mixermode != mode:
                self.new_mixermode(self.mixermode)

    def new_mixermode(self, mode):
        mic = self.mic_opener.any_mic_selected
        sens = (mode == self.NO_PHONE or
                mode == self.PUBLIC_PHONE or
                mic is True)
        self.player_left.listen.set_sensitive(sens)
        self.player_right.listen.set_sensitive(sens)
        self.mic_opener.force_all_on(mode == self.PUBLIC_PHONE)
        if mode == self.PRIVATE_PHONE:
            self.voiplevsbox.show()
            self.spacerbox.show()
            self.voipgainvbox.show()
            self.mixbackvbox.show()
        elif mode == self.PUBLIC_PHONE:
            self.voiplevsbox.show()
            self.spacerbox.show()
            self.voipgainvbox.show()
            self.mixbackvbox.hide()
        else:
            self.voiplevsbox.hide()
            self.spacerbox.hide()

        self.send_new_mixer_stats()

    def cb_crossfade(self, fader):
        cf = fader.get_value()

        if self.metadata_src == self.METADATA_CROSSFADER and (
                cf < 50 <= self.old_cf or self.old_cf < 50 <= cf):
            self.player_left.expire_metadata()
            self.player_right.expire_metadata()

        self.send_new_mixer_stats()

        self.old_cf = cf

    def cb_crosspattern(self, widget):
        print("crossfader pattern changed")
        self.send_new_mixer_stats()

    def cb_deckvol(self, gain):
        self.send_new_mixer_stats()

    def save_session(self, trigger, where=None):
        print("save_session called")

        if where is None:
            session_filename = pm.basedir / self.session_filename
        else:
            where = PathStr(where)
            session_filename = where / self.session_filename

        if trigger in ("atexit", "periodic") and pm.profile is None \
                and pm.session_type != "L0":
            if trigger == "periodic":
                print("periodic save cancelled")
            else:
                print("save at exit blocked")
            # Cancel the periodic timeout with this return value.
            return False

        self.prefs_window.save_resource_template()
        if trigger == "template":
            print("saving template only")
            return True

        try:
            with open(session_filename, "w") as fh:
                fh.write("deckvol=" + str(self.deckadj.get_value()) + "\n")
                fh.write("deck2vol=" + str(self.deck2adj.get_value()) + "\n")
                fh.write("crossfade=" + str(self.crossadj.get_value()) + "\n")
                fh.write("stream_mon=" +
                         str(int(self.listen_stream.get_active())) + "\n")
                fh.write("tracks_played=" +
                         str(int(self.history_expander.get_expanded())) + "\n")
                fh.write("pass_speed=" +
                         str(self.passspeed_adj.get_value()) + "\n")
                fh.write(
                    "prefs={}\n".format(
                        int(self.prefs_window.window.is_visible())
                    )
                )
                fh.write(
                    "server={}\n".format(
                        int(self.prefs_window.window.is_visible())
                    )
                )
                fh.write("prefspage=" +
                         str(self.prefs_window.notebook.get_current_page()) +
                         "\n")
                fh.write("metadata_src=" +
                         str(self.metadata_source.get_active()) + "\n")
                fh.write("crosstype=" +
                         str(self.crosspattern.get_active()) + "\n")
                fh.write("hpane=" +
                         str(self.paned.get_position()) + "\n")
                fh.write("vpane=" +
                         str(self.leftpane.get_position()) + "\n")
                fh.write("cw_tree=" +
                         self.topleftpane.get_col_widths("tree") + "\n")
                fh.write("cw_flat=" +
                         self.topleftpane.get_col_widths("flat") + "\n")
                fh.write("cw_catalogs=" +
                         self.topleftpane.get_col_widths("catalogs") + "\n")
                fh.write("dbpage=" +
                         str(self.topleftpane.notebook.get_current_page()) +
                         "\n")
                fh.write("playerpage=" +
                         str(self.player_nb.get_current_page()) + "\n")
                fh.close()

                # Save a list of files played and timestamps.
                fh = open(session_filename + "_files_played", "wb")
                cutoff = time.time() - 2592000  # 2592000 = 30 days.
                recent = {}
                for key, value in self.files_played.items():
                    if value > cutoff:
                        recent[key] = value
                pickle.Pickler(fh).dump(recent)
                fh.close()

        except Exception as e:
            print("Error writing out main session data", e)

        try:
            fh = open(session_filename + "_tracks", "w")
            start, end = self.history_buffer.get_bounds()
            text = str(self.history_buffer.props.text)
            fh.write(text)
            fh.close()
        except Exception as e:
            print("Error writing out tracks played data", e)

        self.prefs_window.save_player_prefs(where)
        self.controls.save_prefs(where)
        self.server_window.save_session_settings(where)

        # Build links directory when in session mode.
        if pm.profile is None:
            link_uuid_reg.clear()
            for row in itertools.chain(self.player_left.liststore,
                                       self.player_right.liststore,
                                       self.jingles.interlude.liststore):
                uuid_ = row[10]
                try:
                    uuid.UUID(uuid_)
                except:
                    pass
                else:
                    link_uuid_reg.add(uuid_, row[1])

            effects = self.jingles.all_effects
            for uuid_, pathname in ((x.uuid, x.pathname) for x in effects):
                if pathname is not None:
                    link_uuid_reg.add(str(uuid_), pathname)

            link_uuid_reg.update(PathStr(where or pm.basedir) / "links")

        self.player_left.save_session(where)
        self.player_right.save_session(where)
        self.jingles.save_session(where)
        # JACK ports are saved at the moment of change, not here.

        return True  # This is also a timeout routine

    def restore_session(self):
        try:
            fh = open(pm.basedir / self.session_filename, "r")
        except Exception as e:
            print(e)
            return
        while 1:
            try:
                line = fh.readline()
                if line == "":
                    break
            except:
                    break
            k, _, v = line[:-1].partition('=')
            if k == "deckvol":
                self.deckadj.set_value(float(v))
            elif k == "deck2vol":
                self.deck2adj.set_value(float(v))
            elif k == "crossfade":
                self.crossadj.set_value(float(v))
            elif k == "stream_mon":
                self.listen_stream.set_active(int(v))
            elif k == "tracks_played":
                if int(line[14:-1]):
                    self.history_expander.emit("activate")
            elif k == "pass_speed":
                self.passspeed_adj.set_value(float(v))
            elif k == "prefs":
                if v == "1":
                    self.prefs_window.window.show()
            elif k == "server":
                if v == "1":
                    self.server_window.window.show()
            elif k == "jingles":
                if v == "1":
                    self.jingles.show()
            elif k == "prefspage":
                self.prefs_window.notebook.set_current_page(int(v))
            elif k == "metadata_src":
                self.metadata_source.set_active(int(v))
            elif k == "crosstype":
                self.crosspattern.set_active(int(v))
            elif k == "hpane":
                self.paned.set_position(int(v))
            elif k == "vpane":
                self.leftpane.set_position(int(v))
            elif k in ("cw_tree", "cw_flat", "cw_catalogs"):
                self.topleftpane.set_col_widths(k[3:], v)
            elif k == "dbpage":
                self.topleftpane.notebook.set_current_page(int(v))
            elif k == "playerpage":
                self.player_nb.set_current_page(int(v))
        try:
            fh = open(self.session_filename + "_files_played", "r")
        except:
            pass
        else:
            self.files_played = pickle.Unpickler(fh).load()
            fh.close()

        mst = pm.basedir / (self.session_filename + "_tracks")
        try:
            stat = os.stat(mst)
        except OSError as e:
            print(e)
            return
        if stat.st_ctime + 21600 > time.time():
            try:
                fh = open(mst, "r")
            except Exception as e:
                print(e)
                return
            text = fh.read()
            fh.close()
            self.history_buffer.set_text(text)
        else:
            print("disregarding out of date track history text")

    def destroy_hard(self, widget=None, data=None):
        if self.session_loaded:
            self.freewheel_button.set_active(False)
            self.save_session("atexit")
            self.quitting()

        if Gtk.main_level():
            Gtk.main_quit()

        while Gdk.events_pending():
            Gtk.main_iteration()

        time.sleep(0.3)
        exit(5)

    def destroy(self, widget=None, data=None):
        self.freewheel_button.set_active(False)
        self.save_session("atexit")
        if self.crosspass:
            source_remove(self.crosspass)
        self.server_window.cleanup()
        self.mic_opener.close_all()
        self.player_left.cleanup()
        self.player_right.cleanup()
        self.jingles.cleanup()
        self.player_left.flush = True
        self.player_right.flush = True
        self.send_new_mixer_stats()
        self.prefs_window.songdbprefs.disconnect()
        source_remove(self.statstimeout)
        source_remove(self.vutimeout)
        source_remove(self.savetimeout)
        self._mixer_ctrl.close()
        self.quitting()
        self.window.hide()
        self.prefs_window.window.hide()
        self.server_window.window.hide()
        if pm.profile_dialog:
			pm.profile_dialog.hide()

        if Gtk.main_level():
            Gtk.main_quit()

        while Gdk.events_pending():
            Gtk.main_iteration()

        time.sleep(0.3)  # Allow time for all subthreads/programs time to exit
        exit(0)

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename, signature="")
    def quitting(self):
        """Called to notify plugins that this session is closing."""

        pass

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename, signature="")
    def heartbeat(self):
        """Called to notify plugins that this session is healthy."""

        pass

    @dbus.service.signal(dbus_interface=PGlobs.dbus_bus_basename, signature="")
    def tracks_finishing(self):
        """Called to notify DJ that music tracks are ending."""

        pass

    @dbus.service.method(dbus_interface=PGlobs.dbus_bus_basename, out_signature="u")
    def pid(self):
        """Reply with the process ID."""

        return int(os.getpid())

    def delete_event(self, widget, event, data=None):
        qm = ["<span size='12000' weight='bold'>%s</span>" %
              _("Confirmation to quit IDJC is required."), ""]

        if self.server_window.is_streaming and self.server_window.is_recording:
            qm.append(
                _("All active recordings and radio streams will terminate."))
        elif self.server_window.is_streaming:
            qm.append(_("All of the active radio streams will terminate."))
        elif self.server_window.is_recording:
            qm.append(_("All active recordings will cease."))
        else:
            self.destroy()
            return False

        idjc_shutdown_dialog(self.window_group, self.destroy, None, qm)
        return True

    def mixer_write(self, message, target="mx"):
        """The means to communicate with and launch the backend."""

        if target is True or target is False or target is None:
            raise RuntimeError("want traceback")
        try:
            self._mixer_ctrl.write("%s\n%s" % (target, message))
            self._mixer_ctrl.flush()
        except (IOError, ValueError, AttributeError) as e:
            if message == "bootstrap":
                print("launching backend")
            else:
                print(str(e))
            for i in range(1, 4 if self.session_loaded else 2):
                print("backend launch attempt", i)

                read = ctypes.c_int()
                write = ctypes.c_int()
                if not self.backend.init_backend(ctypes.byref(read),
                                                 ctypes.byref(write)):
                    print("call to init_backend failed")
                    continue

                try:
                    self._mixer_ctrl = os.fdopen(write.value, "w")
                    self._mixer_rply = os.fdopen(read.value, "r")
                except OSError:
                    "failed to open streams to backend"
                    continue

                print("awaiting reply")

                for j in range(10):
                    reply = self.mixer_read()
                    print("got", reply)
                    if reply == "idjc backend ready\n":
                        break
                else:
                    print("bad response from newly started backend")
                    continue

                if FGlobs.have_libmpg123:
                    self.mixer_write("ACTN=mp3_getstatus\nend\n")
                    self.mp3status = int(self.mixer_read())

                if message != "bootstrap":
                    # Restore previous settings.
                    self.send_new_mixer_stats()
                    self.prefs_window.mic_controls_backend_update()
                    self.prefs_window.voip_pan_backend_update()
                    self.player_left.next.clicked()
                    self.player_right.next.clicked()
                    self.jingles.interlude.next.clicked()
                    self.server_window.source_client_open()
                    self.comms_reply_pending = False
                    self.server_window.restart_streams_and_recorders()
                    self.jack.restore()
                    self.mixer_write(message, target)
                break
            else:
                print("giving up")
                self.destroy_hard()

    def mixer_read(self, iters=0):
        if iters == 5:
            self.destroy_hard()
        try:
            line = self._mixer_rply.readline()
        except IOError as e:
            print(str(e))
            line = self.mixer_read(iters + 1)
        if line == "Segmentation Fault\n":
            line = ""
            print("Mixer reports a segmentation fault")
            self._mixer_rply.close()
            self._mixer_ctrl.close()
        return line

    def vu_update(self, locking=True, vu_update_counter=[0]):
        session_ns = {}
        player_metadata = []
        midis = ''
        cons_changed = False

        with (gdklock if locking else nullcm)():
            if not Gtk.main_level():
                return False

            vu_update_counter[0] += 1
            if vu_update_counter[0] % 20 == 0:
                self.heartbeat()

            try:
                self.mixer_write("ACTN=requestlevels\nend\n")
            except (ValueError, IOError):
                return True

            while 1:
                line = self.mixer_read().rstrip()
                if line == "":
                    return True

                if line == "end":
                    break

                if not line.count("="):
                    print(line)
                    continue

                key, value = line.split("=", 1)

                if key == "midi":
                    midis = value
                    continue

                if key.startswith("session_"):
                    session_ns[key[8:]] = value
                    continue

                if key == "ports_connections_changed":
                    cons_changed = value != "0"

                if key.endswith("_silence"):
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                else:
                    try:
                        value = int(value)
                    except ValueError:
                        pass

                if key.endswith("_new_metadata"):
                    if not key.startswith("jingles"):
                        if key.startswith("interlude"):
                            target = self.jingles.interlude
                        else:
                            target = getattr(
                                self,
                                "player_" +
                                key.split("_", 1)[0]
                            )
                        player_metadata.append((target, value))
                    continue

                try:
                    self.vumap[key].set_meter_value(value)
                except KeyError:
                    pass
                    # print "key value", key, "missing from vumap"

            if self.jingles.playing is True and \
                    int(self.jingles_playing) == 0:
                self.jingles.clear_indicators()

            for player, data in player_metadata:
                self.update_songname(player, data)

            if midis:
                for midi in midis.split(','):
                    input, _, value = midi.partition(':')
                    self.controls.input(input, int(value, 16))

            if session_ns["command"] == "save_L1" and pm.session_type == "L1":
                self.jack.session_save()
                self.save_session("L1")
            if session_ns["command"].endswith("_JACK") and \
                    pm.session_type == "JACK":
                self.handle_jack_session(**session_ns)

            if cons_changed:
                self.jack.standard_save()

            ep = int(self.effects_playing)
            if ep != -1:
                self.jingles.update_effect_leds(ep)

        return True

    def handle_jack_session(self, command, event, directory, uuid):
        """A JACK session event occurred and the reply data is crafted here."""

        subdir = PathStr(directory) / ("idjc-%s-%s" % (pm.session_type,
                                                       pm.session_name))
        try:
            os.mkdir(subdir)
        except EnvironmentError as e:
            if e.errno != 17:
                print(e)

        command = command.rstrip("_JACK")
        if command in ("save", "saveandexit"):
            self.jack.session_save(subdir)
            self.save_session("JACK", subdir)
        if command == "savetemplate":
            self.save_session("template", subdir)

        commandline = " ".join(
            (
                sys.argv[0],
                "run"
                "--session=JACK:%s:${SESSION_DIR}" % pm.session_name,
                "--jackserver=%s" % uuid
            )
        )

        if args.channels is not None:
            commandline += " -c " + " ".join(args.channels)

        if args.voip is not None:
            commandline += " -V " + args.voip[0]

        if args.servers is not None:
            commandline += " -s " + " ".join(args.servers)

        if args.crossfader is not None:
            commandline += " -x " + args.crossfader[0]

        if args.players is not None:
            commandline += " -P " + " ".join(args.players)

        print("## Restored session commandline will be:", commandline)

        # Reply to backend confirms save has took place.
        self.mixer_write("ACTN=session_reply\nsession_event=%s\n"
                         "session_commandline=%s\n" % (
                             event, commandline))
        self.mixer_read()
        # At this point the session event has been disposed of.

        if command == "saveandquit":
            self.destroy()

    @threadslock
    def stats_update(self):
        players = self.player_left, self.player_right, self.jingles.interlude
        for player in players:
            if player.player_is_playing:
                player.check_mixer_signal()
            elif player.pl_mode.get_active() == 0:
                player.update_time_stats()

        ch = self.mic_opener.mic_list
        for i in range(PGlobs.num_micpairs * 2):
            if self.channel_states[i] == -1:
                ch[i].open.emit("toggled")

        if self.dbus_voip_mode != self.mixermode:
            self.dbus_voip_mode = self.mixermode
            self.voip_mode_changed(self.mixermode)

        return True

    def cb_history_populate(self, textview, menu):
        menusep = Gtk.SeparatorMenuItem()
        menu.append(menusep)
        menusep.show()
        menuitem = Gtk.MenuItem(_('Remove Contents'))
        menuitem.connect_object("activate", Gtk.Button.clicked,
                                self.history_clear)
        menu.append(menuitem)
        menuitem.show()

    def cb_key_capture(self, widget, event):
        if self.topleftpane.in_text_entry():
            return

        self.controls.input_key(event)

    def configure_event(self, widget, event):
        if self.player_left.is_playing:
            self.player_left.reselect_cursor_please = True
        if self.player_right.is_playing:
            self.player_right.reselect_cursor_please = True

    def cb_panehide(self, widget):
        """ hide widget when all it's children are hidden or non existent """
        c1 = widget.get_child1()
        c2 = widget.get_child2()
        if (not c1 or not c1.is_visible()) and \
                (not c2 or not c2.is_visible()):
            widget.hide()

    def strip_focusability(self, widget):
        try:
            widget.forall(self.strip_focusability)
        except AttributeError:
            pass
        widget.set_can_focus(False)

    class initfailed(Exception):

        def __init__(self, errormessage="something bad happened"):
            print(errormessage)

    class initcleanexit(Exception):
        pass

    def flash_test(self):
        """True if the mic button needs to be flashing now or soon."""

        return self.player_left.is_playing or self.player_right.is_playing

    def __init__(self):
        self.appname = PGlobs.app_longform
        self.version = FGlobs.package_version
        self.copyright = PGlobs.copyright
        self.license = PGlobs.license
        self.profile = pm.profile

        signal.signal(signal.SIGINT, self.destroy_hard)
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        signal.signal(signal.SIGUSR2, signal.SIG_IGN)

        socket.setdefaulttimeout(15)

        # Resources to reserve.
        config = configparser.RawConfigParser()
        config.read(pm.basedir / 'config')
        try:
            PGlobs.num_micpairs = config.getint(
                'resource_count',
                'num_micpairs') // 2
        except configparser.Error:
            print("error getting num_micpairs")
        try:
            count = config.getint('resource_count', 'num_streamers')
        except configparser.Error:
            print("error getting num_streamers")
        else:
            PGlobs.num_streamers = count
            PGlobs.num_encoders = count
        try:
            PGlobs.num_recorders = config.getint(
                'resource_count',
                'num_recorders'
            )
        except configparser.Error:
            print("error getting num_recorders")
        try:
            PGlobs.num_effects = config.getint(
                'resource_count',
                'num_effects'
            )
        except configparser.Error:
            pass

        if pm.session_uuid is None:
            try:
                if args.jackserver is not None:
                    os.environ["jack_parameter"] = args.jackserver[0]
            except:
                os.environ["jack_parameter"] = "default"
        else:
            os.environ["jack_parameter"] = pm.session_uuid

        if pm.profile is not None:
            client_id = "idjc_" + pm.profile
        else:
            # Client ID is by session type and name.
            client_id = "idjc_%s_%s" % (pm.session_type, pm.session_name)

        os.environ["app_name"] = "%s (%s) %s" % (PGlobs.app_longform,
                                                 FGlobs.package_name,
                                                 FGlobs.package_version)
        os.environ["client_id"] = client_id
        os.environ["mic_qty"] = str(PGlobs.num_micpairs * 2)
        os.environ["num_streamers"] = str(PGlobs.num_streamers)
        os.environ["num_encoders"] = str(PGlobs.num_encoders)
        os.environ["num_recorders"] = str(PGlobs.num_recorders)
        os.environ["num_effects"] = str(PGlobs.num_effects)
        os.environ["has_head"] = "1"
        os.environ["libmp3lame_filename"] = FGlobs.libmp3lame_filename
        os.environ["libmpg123_filename"] = FGlobs.libmpg123_filename
        # For IPC.
        os.environ["ui2be"] = pm.basedir / "ui2be"
        os.environ["be2ui"] = pm.basedir / "be2ui"

        print("jack client ID:", client_id)

        self.session_loaded = False

        try:
            self.backend = ctypes.CDLL(FGlobs.backend)
        except OSError:
            try:
                subprocess.call(
                    [
                        "notify-send",
                        "-u",
                        "critical",
                        "-a",
                        "IDJC",
                        "IDJC Failed to open %s\n\n"
                        "Cannot continue" % FGlobs.backend
                    ])
            except OSError:
                pass
            raise self.initfailed

        self.mixer_write("bootstrap")

        # create the GUI elements
        self.window_group = Gtk.WindowGroup()
        self.window = Gtk.Window(Gtk.WindowType.TOPLEVEL)
        self.window.set_gravity(Gdk.Gravity.STATIC)
        self.window_group.add_window(self.window)
        self.window.set_title(self.appname + pm.title_extra)
        self.window.connect("delete_event", self.delete_event)
        self.hbox10 = Gtk.Box(homogeneous=False)
        self.hbox10.set_spacing(6)
        self.paned = Gtk.HPaned()
        self.leftpane = Gtk.VPaned()
        self.paned.pack1(self.leftpane)
        self.topleftpane = songdb.MediaPane()
        self.leftpane.pack1(self.topleftpane)
        self.topleftpane.connect_object("show", Gtk.VPaned.show, self.leftpane)
        self.topleftpane.connect_object(
            "hide", self.cb_panehide, self.leftpane)

        # Facility for widget label renaming by the user.
        self.label_subst = LabelSubst(_('Renameable Labels'))

        # Expand features by adding something useful here
        # a dummy widget is needed to prevent a segfault when F8 is pressed
        self.bottomleftpane = Gtk.Button("Bottom")
        self.leftpane.pack2(self.bottomleftpane)

        self.rightpane = Gtk.Box(False, 0)
        self.paned.pack2(self.rightpane, True, False)
        self.vbox8 = Gtk.VBox(False, 0)

        menuhbox = Gtk.Box()
        self.vbox8.pack_start(menuhbox, False, False, 0)
        menuhbox.show()
        self.menu = MainMenu()
        menuhbox.pack_start(self.menu, True, True, 0)
        self.menu.show()
        self.rightpane.pack_start(self.vbox8, True, True, 0)
        self.window.add(self.paned)
        self.rightpane.show()
        self.paned.show()

        self.player_nb = Gtk.Notebook()
        main_label = Gtk.Label(_('Main Players'))
        self.label_subst.add_widget(
            main_label,
            "mainplayerslabel",
            _('Main Players')
        )
        self.vbox6 = Gtk.VBox(False, 0)
        self.player_nb.append_page(self.vbox6, main_label)
        main_label.show()
        self.vbox8.pack_start(self.player_nb, True, True, 0)
        self.player_nb.show()
        self.hbox7 = Gtk.Box(True)
        self.hbox10.show()

        self.hbox10spc = Gtk.Box()
        self.vbox8.pack_start(self.hbox10spc, False, False, 3)
        self.hbox10spc.show()

        self.vbox8.pack_start(self.hbox10, False, False, 0)

        spc = Gtk.Box()
        self.vbox8.pack_start(spc, False, False, 2)
        spc.show()

        # show box 8 now that it's finished
        self.vbox8.show()

        self.freewheel_button = FreewheelButton(self.mixer_write)
        self.hbox10.pack_start(self.freewheel_button, False, False, 0)

        self.dsp_button = Gtk.ToggleButton()
        self.dsp_button.viewlevels = (5,)
        label = Gtk.Label()
        label.set_markup(
            "<span weight='bold' size='9000' "
            "foreground='#333'>%s</span>" % _('DSP')
        )
        self.dsp_button.add(label)
        label.show()
        self.dsp_button.connect(
            "toggled",
            lambda w: self.send_new_mixer_stats()
        )
        self.hbox10.pack_start(self.dsp_button, False, False, 0)
        self.dsp_button.show()

        phonebox = Gtk.Box()
        phonebox.viewlevels = (5,)
        phonebox.set_spacing(2)

        pixbuf4 = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "greenphone.png")
        pixbuf4 = pixbuf4.scale_simple(25, 20, GdkPixbuf.InterpType.BILINEAR)
        image = Gtk.Image()
        image.set_from_pixbuf(pixbuf4)
        image.show()
        self.greenphone = Gtk.ToggleButton()
        self.greenphone.add(image)
        self.greenphone.connect("toggled", self.cb_toggle, "Greenphone")
        phonebox.pack_start(self.greenphone, True, True, 0)
        self.greenphone.show()
        set_tip(
            self.greenphone,
            _('Mix voice over IP audio to the output stream.'))

        pixbuf5 = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "redphone.png"
        )
        pixbuf5 = pixbuf5.scale_simple(25, 20, GdkPixbuf.InterpType.BILINEAR)
        image = Gtk.Image()
        image.set_from_pixbuf(pixbuf5)
        image.show()
        self.redphone = Gtk.ToggleButton()
        self.redphone.add(image)
        self.redphone.connect("toggled", self.cb_toggle, "Redphone")
        phonebox.pack_start(self.redphone, True, True, 0)
        self.redphone.show()
        set_tip(self.redphone, _('Mix voice over IP audio to the DJ only.'))

        self.hbox10.pack_start(phonebox, False, False, 0)
        phonebox.show()

        self.pan_preset_chooser = PanPresetChooser()
        self.pan_preset_chooser.viewlevels = (5,)
        self.hbox10.pack_start(self.pan_preset_chooser, False, False, 0)
        self.pan_preset_chooser.show_all()

        # microphone open/unmute dynamic widget cluster thingy
        self.mic_opener = MicOpener(self, self.flash_test)
        self.mic_opener.viewlevels = (5,)
        self.hbox10.pack_start(self.mic_opener, True, True, 0)
        self.mic_opener.show()

        # playlist advance button
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "advance.png"
        )
        pixbuf = pixbuf.scale_simple(32, 14, GdkPixbuf.InterpType.BILINEAR)
        image = Gtk.Image()
        image.set_from_pixbuf(pixbuf)
        self.advance = Gtk.Button()
        self.advance.add(image)
        image.show()
        self.advance.connect("clicked", self.callback, "Advance")
        self.hbox10.pack_end(self.advance, False, False, 0)
        self.advance.show()
        set_tip(
            self.advance,
            _('This button steps through the active playlist,'
              ' pausing between tracks. The active playlist is'
              ' defined by the placement of the crossfader.'))

        self.hbox7.show()
        self.hbox10.show()

        self.hbox4 = Gtk.Box(False, 0)
        self.vbox6.pack_start(self.hbox4, True, True, 0)

        # Boxes 3L and 3R contain our media players
        self.vbox3L = Gtk.VBox(False, 0)
        self.vbox3L.set_border_width(2)
        self.hbox4.pack_start(self.vbox3L, True, True, 0)

        # A vertical box for our main volume controls
        self.vboxvol = Gtk.VBox(False, 0)
        self.vboxvol.set_border_width(2)
        self.volframe = Gtk.Frame()
        self.volframe.viewlevels = (5,)
        self.volframe.set_border_width(5)
        self.volframe.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        self.volframe.add(self.vboxvol)
        self.volframe.show()
        self.hbox4.pack_start(self.volframe, False, True, 3)

        # A pictoral volume label above horizontally-stacked volume control(s)
        image = Gtk.Image()
        image.set_from_file(FGlobs.pkgdatadir / "volume2.png")
        self.vboxvol.pack_start(image, False, False, 0)
        image.show()
        hboxvol = Gtk.Box(True, 0)
        self.vboxvol.pack_start(hboxvol, True, True, 0)
        hboxvol.show()

        # Primary volume control
        self.deckadj = Gtk.Adjustment(127.0, 0.0, 127.0, 1.0, 6.0)
        self.deckadj.connect("value_changed", self.cb_deckvol)
        self.deckvol = Gtk.VScale(adjustment=self.deckadj)
        # self.deckvol.set_update_policy(Gtk.UPDATE_CONTINUOUS)
        self.deckvol.set_draw_value(False)
        self.deckvol.set_inverted(True)
        hboxvol.pack_start(self.deckvol, False, False, 4)
        self.deckvol.show()
        set_tip(
            self.deckvol,
            _('The volume control shared by both music players.'))

        # Visible when using separate player volume controls.
        self.deck2adj = Gtk.Adjustment(127.0, 0.0, 127.0, 1.0, 6.0)
        self.deck2adj.connect("value_changed", self.cb_deckvol)
        self.deck2vol = Gtk.VScale(adjustment=self.deck2adj)
        # self.deck2vol.set_update_policy(Gtk.UPDATE_CONTINUOUS)
        self.deck2vol.set_draw_value(False)
        self.deck2vol.set_inverted(True)
        hboxvol.pack_start(self.deck2vol, False, False, 0)
        set_tip(
            self.deck2vol,
            _('The volume control for the right music player.'))

        self.spacerbox = Gtk.VBox()
        self.vboxvol.pack_start(self.spacerbox, False, False, 3)

        self.voiplevsbox = Gtk.Box(True, 0)
        self.vboxvol.pack_start(self.voiplevsbox, True, True, 0)

        self.voipgainvbox = Gtk.VBox()
        self.voipgainvbox.set_spacing(1)
        self.voiplevsbox.pack_start(self.voipgainvbox, False, False, 0)
        self.voipgainvbox.show()

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "greenphone.png"
        )
        pixbuf = pixbuf.scale_simple(20, 17, GdkPixbuf.InterpType.HYPER)
        greenphoneimage = Gtk.Image()
        greenphoneimage.set_from_pixbuf(pixbuf)
        self.voipgainvbox.pack_start(greenphoneimage, False, False, 0)
        greenphoneimage.show()

        self.voipgainadj = Gtk.Adjustment(64.0, 0.0, 127.0, 1.0, 6.0)
        self.voipgainadj.connect("value_changed", self.cb_deckvol)
        voipgain = Gtk.VScale(adjustment=self.voipgainadj)
        # voipgain.set_update_policy(Gtk.UPDATE_CONTINUOUS)
        voipgain.set_draw_value(False)
        voipgain.set_inverted(True)
        self.voipgainvbox.pack_start(voipgain, True, True, 0)
        voipgain.show()
        set_tip(
            self.voipgainvbox,
            _('VoIP level adjustment. 0dB gain is at the mid point.'))

        self.mixbackvbox = Gtk.VBox()
        self.mixbackvbox.set_spacing(1)
        self.voiplevsbox.pack_start(self.mixbackvbox, False, False, 0)
        self.mixbackvbox.show()

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "pbphone.png"
        )
        pixbuf = pixbuf.scale_simple(20, 17, GdkPixbuf.InterpType.HYPER)
        pbphoneimage = Gtk.Image()
        pbphoneimage.set_from_pixbuf(pixbuf)
        self.mixbackvbox.pack_start(pbphoneimage, False, False, 0)
        pbphoneimage.show()

        self.mixbackadj = Gtk.Adjustment(64.0, 0.0, 127.0, 1.0, 6.0)
        self.mixbackadj.connect("value_changed", self.cb_deckvol)
        mixback = Gtk.VScale(adjustment=self.mixbackadj)
        # mixback.set_update_policy(Gtk.UPDATE_CONTINUOUS)
        mixback.set_draw_value(False)
        mixback.set_inverted(True)
        self.mixbackvbox.pack_start(mixback, True, True, 0)
        mixback.show()
        set_tip(
            self.mixbackvbox,
            _('The stream volume level to send to '
              'the voice over IP connection.'))

        self.vboxvol.show()

        # A box for the second deck.
        self.vbox3R = Gtk.VBox(False, 0)
        self.vbox3R.viewlevels = (5,)
        self.vbox3R.set_border_width(2)
        self.hbox4.pack_start(self.vbox3R, True, True, 0)

        # hbox4 is full now so let's show it.
        self.hbox4.show()

        # The contents of the two player panes 3L and 3R are next up
        # The two identical players have been moved into one class

        self.player_left = IDJC_Media_Player(self.vbox3L, "left", self)
        self.vbox3L.show()

        self.player_right = IDJC_Media_Player(self.vbox3R, "right", self)
        self.vbox3R.show()

        # A track history window to help with announcements

        history_expander_hbox = Gtk.Box()
        # Expander widget text for indicating recent tracks played.
        self.history_expander = Gtk.Expander.new_with_mnemonic(
            _('Tracks Played'))
        history_expander_hbox.pack_start(self.history_expander, True, True, 6)
        self.history_expander.connect(
            "notify::expanded",
            self.expandercallback
        )
        self.history_expander.show()
        self.vbox6.pack_start(history_expander_hbox, False, False, 0)
        history_expander_hbox.show()

        self.history_vbox = Gtk.VBox()
        history_hbox = Gtk.Box()
        self.history_vbox.pack_start(history_hbox, True, True, 0)
        self.vbox6.pack_start(self.history_vbox, True, True, 0)
        history_hbox.show()
        history_frame = Gtk.Frame()
        history_hbox.pack_start(history_frame, True, True, 6)
        history_frame.show()
        history_frame.set_border_width(0)
        self.history_window = Gtk.ScrolledWindow()
        history_frame.add(self.history_window)
        self.history_window.set_border_width(4)
        self.history_window.show()
        self.history_window.set_size_request(-1, 81)
        self.history_window.set_shadow_type(Gtk.ShadowType.IN)
        self.history_window.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.ALWAYS
        )

        history_clear_box = Gtk.Box()
        # TC: Popup menu item, wipes away the tracks played history text.
        self.history_clear = Gtk.Button(" " + _('Remove Contents') + " ")
        self.history_clear.connect("clicked", self.callback, "Clear History")
        history_clear_box.pack_start(self.history_clear, True, False, 0)
        self.history_clear.show()
        self.history_vbox.pack_start(history_clear_box, False, False, 1)

        spacer = Gtk.VBox()
        self.history_vbox.pack_start(spacer, False, False, 1)
        spacer.show()

        self.history_textview = Gtk.TextView()
        self.history_textview.connect(
            "populate-popup",
            self.cb_history_populate
        )
        self.history_window.add(self.history_textview)
        self.history_textview.show()
        self.history_textview.set_cursor_visible(False)
        self.history_textview.set_editable(False)
        self.history_textview.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.history_buffer = self.history_textview.get_buffer()

        self.abox = Gtk.Box()
        self.abox.viewlevels = (5,)
        self.abox.set_border_width(2)
        self.vbox6.pack_start(self.abox, False, False, 0)
        self.abox.show()

        # The crossfader.  No DJ should be without one. ;)
        self.outercrossbox = Gtk.Box()

        self.outercrossbox.viewlevels = (5,)
        crossframe = Gtk.Frame()
        self.outercrossbox.pack_start(crossframe, True, True, 6)
        self.outercrossbox.show()
        crossframe.set_border_width(0)
        self.crossbox = Gtk.Box()
        crossframe.add(self.crossbox)
        crossframe.show()
        self.crossbox.set_border_width(2)
        self.crossbox.set_spacing(3)

        cross_sizegroup = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)
        cross_sizegroup2 = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)
        sg3 = Gtk.SizeGroup(Gtk.SizeGroupMode.VERTICAL)

        smvbox = Gtk.VBox()
        label = Gtk.Label(label=_('Monitor Mix'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Monitor')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        smvbox.add(label)
        label.show()

        smhbox = Gtk.Box()
        smhbox.set_border_width(1)
        self.listen_dj = Gtk.RadioButton(None, label=_('DJ'))
        smhbox.add(self.listen_dj)
        self.listen_dj.show()
        self.listen_stream = Gtk.RadioButton(None, label=_('Stream'))
        smhbox.add(self.listen_stream)
        self.listen_stream.join_group(self.listen_dj)
        self.listen_stream.show()
        smhbox.show()
        smvbox.add(smhbox)
        sg3.add_widget(smhbox)

        self.listen_stream.connect("toggled", self.cb_toggle, "stream-mon")
        # TC: Context {0}, {1}, {2} = Monitor Mix, Stream, DJ
        # TC: Or whatever they become translated to.
        set_tip(
            smvbox,
            _(
                "In IDJC there are are two audio paths and this '{0}'"
                " control toggles between them. When '{1}' is active you can "
                "hear what the listeners are hearing including the "
                "effects of the crossfader. '{0}' needs to be set to "
                "'{2}' in order to make proper use of the "
                "VoIP features."
            ).format(
                _("Monitor Mix"),
                _("Stream"),
                _("DJ")
            )
        )

        cross_sizegroup.add_widget(smhbox)
        self.crossbox.pack_start(smvbox, False, False, 0)
        smvbox.show()

        # metadata source selector combo box
        mvbox = Gtk.VBox()
        # TC: Dropdown box title text widget.
        label = Gtk.Label(label=_('Metadata Source'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Metadata')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        mvbox.add(label)
        label.show()
        self.metadata_source = Gtk.ComboBoxText()
        # TC: The chosen source of track metadata.
        self.metadata_source.append_text(_('Playlist 1'))
        # TC: The chosen source of track metadata.
        self.metadata_source.append_text(_('Playlist 2'))
        # TC: The chosen source of track metadata.
        self.metadata_source.append_text(_('Last Played'))
        # TC: The chosen source of track metadata.
        self.metadata_source.append_text(_('Crossfader'))
        # TC: The chosen source of track metadata. In this case no metadata.
        self.metadata_source.append_text(_('None'))
        # TC: The chosen source of track metadata. In this case no metadata.
        self.metadata_source.append_text(_('Playlist 3'))
        self.metadata_source.set_active(3)
        cross_sizegroup.add_widget(self.metadata_source)
        self.metadata_source.connect("changed", self.cb_metadata_source)
        set_tip(
            self.metadata_source,
            _('Select the origin for the playing '
              'track metadata on the stream.')
        )
        mvbox.add(self.metadata_source)
        self.metadata_source.show()
        self.crossbox.pack_start(mvbox, False, False, 0)
        mvbox.show()
        cross_sizegroup2.add_widget(self.metadata_source)
        sg3.add_widget(self.metadata_source)

        plvbox = Gtk.VBox()
        # TC: Abbreviation of left.
        label = Gtk.Label(label=_('L'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('L')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        plvbox.add(label)
        label.show()
        self.passleft = make_arrow_button(
            self,
            Gtk.ArrowType.LEFT,
            Gtk.ShadowType.NONE,
            "cfmleft"
        )
        plvbox.add(self.passleft)
        self.passleft.show()
        self.crossbox.pack_start(plvbox, False, False, 0)
        plvbox.show()
        set_tip(plvbox, _('Move the crossfader fully left.'))
        sg3.add_widget(self.passleft)

        self.crossadj = Gtk.Adjustment(0.0, 0.0, 100.0, 1.0, 3.0, 0.0)
        self.crossadj.connect("value_changed", self.cb_crossfade)
        cvbox = Gtk.VBox()
        label = Gtk.Label(label=_('Crossfader'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Crossfader')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        cvbox.add(label)
        label.show()
        self.crossfade = Gtk.HScale(adjustment=self.crossadj)
        # self.crossfade.set_update_policy(Gtk.UPDATE_CONTINUOUS)
        self.crossfade.set_draw_value(False)
        cvbox.add(self.crossfade)
        self.crossfade.show()
        self.crossbox.pack_start(cvbox, True, True, 0)
        cvbox.show()
        self.vbox6.pack_start(self.outercrossbox, False, False, 2)
        set_tip(cvbox, _('The crossfader.'))

        prvbox = Gtk.VBox()
        # TC: Abbreviation of right.
        label = Gtk.Label(label=_('R'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('R')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        prvbox.add(label)
        label.show()
        self.passright = make_arrow_button(
            self,
            Gtk.ArrowType.RIGHT,
            Gtk.ShadowType.NONE,
            "cfmright"
        )
        prvbox.add(self.passright)
        self.passright.show()
        self.crossbox.pack_start(prvbox, False, False, 0)
        prvbox.show()
        set_tip(prvbox, _('Move the crossfader fully right.'))
        sg3.add_widget(self.passright)

        patternbox = Gtk.Box()
        patternbox.set_spacing(2)
        sg4 = Gtk.SizeGroup(Gtk.SizeGroupMode.VERTICAL)

        passbox = Gtk.VBox()
        # TC: Describes a mid point.
        label = Gtk.Label(label=_('Middle'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Middle')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        label.show()
        passbox.add(label)
        passhbox = Gtk.Box()
        passhbox.set_spacing(2)
        passbox.add(passhbox)
        passhbox.show()
        patternbox.pack_start(passbox, False, False, 0)
        passbox.show()

        self.passmidleft = make_arrow_button(
            self,
            Gtk.ArrowType.UP,
            Gtk.ShadowType.NONE,
            "cfmmidl"
        )
        sg4.add_widget(self.passmidleft)
        passhbox.pack_start(self.passmidleft, False, False, 0)
        self.passmidleft.show()
        set_tip(
            self.passmidleft,
            _('Move the crossfader to the '
              'middle of its range of travel.'))

        self.passmidright = make_arrow_button(
            self,
            Gtk.ArrowType.UP,
            Gtk.ShadowType.NONE,
            "cfmmidr"
        )
        passhbox.pack_start(self.passmidright, False, False, 0)
        self.passmidright.show()
        set_tip(self.passmidright,
                _('Move the crossfader to the middle of its range of travel.'))
        sg4.add_widget(self.passmidright)

        pvbox = Gtk.VBox()
        # TC: The attenuation response curve of the crossfader.
        # User selectable.
        label = Gtk.Label(label=_('Response'))
        label.set_attributes(
            Pango.parse_markup(
                '<span size="{}">{}</span>'.format(
                    METER_TEXT_SIZE,
                    _('Response')
                ),
                -1,
                "0")[1]
        )
        pvbox.add(label)
        label.show()
        liststore = Gtk.ListStore(GdkPixbuf.Pixbuf)
        self.crosspattern = Gtk.ComboBox()
        self.crosspattern.set_model(liststore)
        cell = Gtk.CellRendererPixbuf()
        self.crosspattern.pack_start(cell, True)
        self.crosspattern.add_attribute(cell, 'pixbuf', 0)
        liststore.append(
            (GdkPixbuf.Pixbuf.new_from_file(
                FGlobs.pkgdatadir / "classic_cross.png"), ))
        liststore.append(
            (GdkPixbuf.Pixbuf.new_from_file(
                FGlobs.pkgdatadir / "mk2_cross.png"), ))
        liststore.append(
            (GdkPixbuf.Pixbuf.new_from_file(
                FGlobs.pkgdatadir / "pat3.png"), ))
        pvbox.pack_start(self.crosspattern, True, True, 0)
        self.crosspattern.show()
        self.crossbox.pack_start(patternbox, False, False, 0)
        patternbox.show()
        cross_sizegroup2.add_widget(patternbox)
        self.crosspattern.set_active(0)
        self.crosspattern.connect("changed", self.cb_crosspattern)
        set_tip(
            self.crosspattern,
            _('This selects the response curve of the '
              'crossfader.\n\nThe mid-point attenuations are '
              '-3dB, 0dB, and -22dB '
              'respectively.')
        )

        patternbox.pack_start(pvbox, True, True, 0)
        pvbox.show()

        sg4.add_widget(self.crosspattern)

        passbox = Gtk.Box()
        passbox.set_spacing(2)

        tvbox = Gtk.VBox()
        # TC: Duration in seconds.
        label = Gtk.Label(label=_('Time'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Time')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        tvbox.add(label)
        label.show()
        self.passspeed_adj = Gtk.Adjustment(1.0, 0.25, 20.0, 0.25, 0.25)
        psvbox = Gtk.VBox()
        hs = Gtk.HSeparator()
        psvbox.pack_start(hs, False, False, 0)
        hs.show()
        self.passspeed = Gtk.SpinButton(
            adjustment=self.passspeed_adj,
            climb_rate=0,
            digits=2
        )
        psvbox.pack_start(self.passspeed, True, False, 0)
        self.passspeed.show()
        hs = Gtk.HSeparator()
        psvbox.pack_start(hs, False, False, 0)
        hs.show()
        tvbox.pack_start(psvbox, False, False, 0)
        psvbox.show()
        set_tip(
            tvbox,
            _('The time in seconds that the crossfader will take to'
              ' automatically pass across when the button to the '
              'right is clicked.')
        )
        passbox.pack_start(tvbox, False, False, 0)
        tvbox.show()
        sg4.add_widget(psvbox)

        pvbox = Gtk.VBox()
        # TC: The crossfader pass-across button text.
        # TC: The actual button appears as [<-->] with this text above it.
        label = Gtk.Label(label=_('Pass'))
        attrlist = Pango.parse_markup(
            '<span size="{}">{}</span>'.format(
                METER_TEXT_SIZE,
                _('Pass')
            ),
            -1,
            "0")[1]
        label.set_attributes(attrlist)
        pvbox.add(label)
        label.show()
        image = Gtk.Image()
        image.set_from_file(FGlobs.pkgdatadir / "pass.png")
        image.show()
        self.passbutton = Gtk.Button()
        self.passbutton.set_size_request(53, -1)
        self.passbutton.add(image)
        self.passbutton.connect("clicked", self.callback, "pass-crossfader")
        pvbox.add(self.passbutton)
        self.passbutton.show()
        set_tip(
            pvbox,
            _('This button causes the crossfader to move to the '
              'opposite side at a speed determined by the speed'
              ' selector to the left.')
        )
        passbox.pack_start(pvbox, True, True, 0)
        pvbox.show()
        sg4.add_widget(self.passbutton)

        self.crossbox.pack_start(passbox, False, False, 0)
        cross_sizegroup.add_widget(passbox)
        passbox.show()
        self.crossbox.show()

        abox = Gtk.Box()
        abox.set_border_width(1)
        self.vbox6.pack_start(abox, False, False, 0)
        abox.show()

        # We are done with vbox6 so lets show it
        self.vbox6.show()

        # The various meters
        self.metereventbox = Gtk.EventBox()
        self.metereventbox.viewlevels = (5,)
        self.meterbox = Gtk.Box()
        self.metereventbox.add(self.meterbox)
        self.rightpane.pack_start(self.metereventbox, False, False, 0)
        self.meterbox.show()
        self.metereventbox.show()

        # Box contains stream peak, vu and connection status, listener stats.
        self.streammeterbox = PaddedVBox(3, 2, 0, 0, 5)
        self.meterbox.pack_start(self.streammeterbox, False, False, 0)
        self.streammeterbox.show()

        # Table that contains 1, 2, or 4 microphone meters.
        self.micmeterbox = PaddedVBox(3, 2, 0, 0, 5)
        self.meterbox.pack_start(self.micmeterbox, False, False, 0)
        self.micmeterbox.show()

        self.str_l_peak = peakholdmeter()
        self.str_r_peak = peakholdmeter()

        # TC: This text appears above the stream mix peak level meter.
        self.stream_peak_box = make_meter_unit(
            _('Peak'), self.str_l_peak, self.str_r_peak)
        self.streammeterbox.pack_start(self.stream_peak_box, True, True, 0)
        self.stream_peak_box.show()
        set_tip(
            self.stream_peak_box,
            _('A peak hold meter indicating the '
              'signal strength of the stream audio.')
        )

        sg = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)
        self.stream_indicator = []

        for i in range(PGlobs.num_streamers):
            self.stream_indicator.append(StreamMeter(1, 100))

        self.stream_indicator_box, self.listener_indicator = \
            make_stream_meter_unit(_('Streams'), self.stream_indicator)

        self.streammeterbox.pack_start(
            self.stream_indicator_box, False, False, 0)

        self.stream_indicator_box.show()
        sg.add_widget(self.stream_indicator_box)

        if PGlobs.num_recorders:
            self.recording_panel = RecordingPanel(PGlobs.num_recorders)
            self.streammeterbox.pack_start(
                self.recording_panel,
                False,
                False,
                0
            )
            self.recording_panel.show()

        self.str_l_rms_vu = vumeter()
        self.str_r_rms_vu = vumeter()
        # TC: This text appears above the stream mix VU meter.
        stream_vu_box = make_meter_unit(_('VU'),
                                        self.str_l_rms_vu, self.str_r_rms_vu)
        self.streammeterbox.pack_start(stream_vu_box, True, True, 0)
        stream_vu_box.show()
        set_tip(stream_vu_box, _('A VU meter for the stream audio.'))

        # TC: Appears above the mic meters as a label followed by a number.
        self.mic_meters = [
            MicMeter(_("Ch"), i)
            for i in range(1, PGlobs.num_micpairs * 2 + 1)
        ]
        if len(self.mic_meters) <= 4:
            for meter in self.mic_meters:
                self.micmeterbox.pack_start(meter, True, True, 0)
                meter.show()
        else:
            chvbox = Gtk.VBox()
            chvbox.set_spacing(4)
            self.micmeterbox.pack_start(chvbox, True, True, 0)
            chvbox.show()

            def showhide(widget, state, box, l, r):
                # if l.flags() & Gtk.SENSITIVE or r.flags() & Gtk.SENSITIVE:
                if l.is_sensitive() or r.is_sensitive():
                    box.show()
                else:
                    box.hide()

            for l, r in zip(*((iter(self.mic_meters),) * 2)):
                chhbox = Gtk.Box()
                chhbox.set_spacing(4)
                chhbox.pack_start(l, False, False, 0)
                chhbox.pack_end(r, False, False, 0)
                chvbox.pack_start(chhbox, True, True, 0)
                chhbox.show()
                for each in l, r:
                    each.connect("state-changed", showhide, chhbox, l, r)
                    each.show()

        set_tip(
            self.micmeterbox,
            _('A peak hold meter indicating the microphone '
                'signal strength and a meter indicating attenuation '
                'levels in the microphone signal processing system. Green '
                'indicates attenuation from the noise gate, yellow from the '
                'de-esser, red from the limiter.'
              )
        )

        # Aux players initialisation.
        self.jingles = ExtraPlayers(self)
        self.player_nb.append_page(self.jingles, self.jingles.nb_label)
        self.player_nb.set_current_page(0)

        # Variable initialisation
        self.songname = ""
        self.newmetadata = False
        self.showing_left_file_requester = False
        self.showing_right_file_requester = False
        self.old_metadata = None
        self._old_metadata_2 = None
        self.simplemixer = False
        self.crosspass = 0
        self.old_meta_context = None
        self.old_cf = 0

        # initialize metadata source setting
        self.last_player = ""
        self.METADATA_LEFT_DECK = 0
        self.METADATA_RIGHT_DECK = 1
        self.METADATA_LAST_PLAYED = 2
        self.METADATA_CROSSFADER = 3
        self.METADATA_NONE = 4
        self.METADATA_BACKGROUND = 5
        self.metadata_src = self.METADATA_CROSSFADER

        self.alarm = False
        self.NO_PHONE = 0
        self.PUBLIC_PHONE = 1
        self.PRIVATE_PHONE = 2
        self.mixermode = self.NO_PHONE
        self.jingles_playing = SlotObject(0)
        self.interlude_playing = SlotObject(0)
        self.player_left.playtime_elapsed = SlotObject(0)
        self.player_right.playtime_elapsed = SlotObject(0)
        self.jingles.interlude.playtime_elapsed = SlotObject(0)
        self.player_left.mixer_playing = SlotObject(0)
        self.player_right.mixer_playing = SlotObject(0)
        self.jingles.interlude.mixer_playing = SlotObject(0)
        self.player_left.mixer_signal_f = SlotObject(0)
        self.player_right.mixer_signal_f = SlotObject(0)
        self.jingles.interlude.mixer_signal_f = SlotObject(0)
        self.player_left.mixer_cid = SlotObject(0)
        self.player_right.mixer_cid = SlotObject(0)
        self.jingles.interlude.mixer_cid = SlotObject(0)
        self.left_compression_level = SlotObject(0)
        self.right_compression_level = SlotObject(0)
        self.left_deess_level = SlotObject(0)
        self.right_deess_level = SlotObject(0)
        self.left_noisegate_level = SlotObject(0)
        self.right_noisegate_level = SlotObject(0)
        self.jingles.mixer_jingles_cid = SlotObject(0)
        self.jingles.mixer_interlude_cid = SlotObject(0)
        self.player_left.runout = SlotObject(0)
        self.player_right.runout = SlotObject(0)
        self.jingles.interlude.runout = SlotObject(0)
        self.metadata_left_ctrl = SlotObject(0)
        self.metadata_right_ctrl = SlotObject(0)
        self.metadata_interlude_ctrl = SlotObject(0)
        self.player_left.silence = SlotObject(0.0)
        self.player_right.silence = SlotObject(0.0)
        self.jingles.interlude.silence = SlotObject(0.0)
        self.sample_rate = SlotObject(0)
        self.effects_playing = SlotObject(0)
        self.channel_states = [-1, ] * 12
        self.dbus_voip_mode = -1

        self.feature_set = Gtk.ToggleButton()
        self.feature_set.set_active(True)
        self.feature_set.connect("toggled", self.callback, "Features")

        self.full_wst = WindowSizeTracker(self.window, True)
        self.min_wst = WindowSizeTracker(self.window, False)

        self.in_vu_timeout = False
        self.vucounter = 0
        self.session_filename = "main_session"
        self.files_played = {}
        self.files_played_offline = {}

        # Variable map for stuff read from the mixer
        self.vumap = {
            "str_l_peak": self.str_l_peak,
            "str_r_peak": self.str_r_peak,
            "str_l_rms": self.str_l_rms_vu,
            "str_r_rms": self.str_r_rms_vu,
            "left_elapsed": self.player_left.playtime_elapsed,
            "right_elapsed": self.player_right.playtime_elapsed,
            "interlude_elapsed": self.jingles.interlude.playtime_elapsed,
            "left_playing": self.player_left.mixer_playing,
            "right_playing": self.player_right.mixer_playing,
            "jingles_playing": self.jingles_playing,
            "interlude_playing": self.jingles.interlude.mixer_playing,
            "left_signal": self.player_left.mixer_signal_f,
            "right_signal": self.player_right.mixer_signal_f,
            "interlude_signal": self.jingles.interlude.mixer_signal_f,
            "left_cid": self.player_left.mixer_cid,
            "right_cid": self.player_right.mixer_cid,
            "jingles_cid": self.jingles.mixer_jingles_cid,
            "interlude_cid": self.jingles.interlude.mixer_cid,
            "left_audio_runout": self.player_left.runout,
            "right_audio_runout": self.player_right.runout,
            "interlude_audio_runout": self.jingles.interlude.runout,
            "left_additional_metadata": self.metadata_left_ctrl,
            "right_additional_metadata": self.metadata_right_ctrl,
            "interlude_additional_metadata": self.metadata_interlude_ctrl,
            "left_silence": self.player_left.silence,
            "right_silence": self.player_right.silence,
            "interlude_silence": self.jingles.interlude.silence,
            "sample_rate": self.sample_rate,
            "effects_playing": self.effects_playing,
            "freewheel_mode": self.freewheel_button
        }

        for i, mic in enumerate(self.mic_meters):
            self.vumap.update({"mic_%d_levels" % (i + 1): mic})

        self.controls = midicontrols.Controls(self)
        self.controls.load_prefs()

        self.window.realize()
        media_sg = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)
        media_sg.add_widget(self.vbox3L)
        media_sg.add_widget(self.vbox3R)

        self.menu.playersmenu_i.set_active(True)
        self.menu.playersmenu_i.connect(
            "activate",
            lambda w: self.player_nb.set_visible(w.get_active())
        )

        self.menu.quitmenu_i.connect_object(
            "activate",
            self.delete_event,
            self.window,
            None
        )

        self.menu.outputmenu_i.connect(
            "activate",
            lambda w: self.server_window.window.present()
        )

        self.menu.prefsmenu_i.connect(
            "activate",
            lambda w: self.prefs_window.window.present()
        )
        if pm.profile is not None:
            self.menu.profilesmenu_i.connect(
                "activate",
                lambda w: pm.profile_dialog.present()
            )
        else:
            self.menu.profilesmenu_i.set_sensitive(False)
        self.menu.aboutmenu_i.connect(
            "activate",
            lambda w: self.prefs_window.show_about()
        )

        self.jack = JackMenu(
            self.menu,
            lambda s, r: self.mixer_write(
                "ACTN=jack%s\n%s" % (s, r)),
            lambda: self.mixer_read()
        )
        self.jack.load(startup=True)

        self.server_window = SourceClientGui(self)
        self.prefs_window = mixprefs(self)
        self.prefs_window.load_player_prefs()
        self.prefs_window.apply_player_prefs()

        self.vutimeout = timeout_add(50, self.vu_update)
        self.statstimeout = timeout_add(100, self.stats_update)

        self.savetimeout = timeout_add_seconds(
            120,
            threadslock(self.save_session),
            "periodic"
        )

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(
                sig,
                lambda s, f: idle_add(threadslock(self.destroy))
            )

        (self.full_wst, self.min_wst)[bool(self.simplemixer)].apply()
        self.window.connect("configure_event", self.configure_event)
        self.jingles.interlude.listen.set_active(False)

        if self.prefs_window.restore_session_option.get_active():
            print("Restoring previous session")
            self.player_left.restore_session()
            self.player_right.restore_session()
            self.jingles.restore_session()
            self.restore_session()
        self.session_loaded = True

        self.window.set_focus_chain(
            (
                self.player_left.scrolllist,
                self.player_right.scrolllist,
                self.jingles.interlude.scrolllist
            )
        )

        self.server_window.update_metadata()

        self.window.forall(self.strip_focusability)
        self.topleftpane.repair_focusability()
        self.player_left.treeview.set_can_focus(True)
        self.player_right.treeview.set_can_focus(True)
        self.jingles.interlude.treeview.set_can_focus(True)
        self.player_left.treeview.grab_focus()

        self.window.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.window.connect("key-press-event", self.cb_key_capture)
        self.window.connect("key-release-event", self.cb_key_capture)

        self.window.show()
        idle_add(lambda: self.prefs_window.window.realize() and False)

        self.player_left.treeview.emit("cursor-changed")
        self.player_right.treeview.emit("cursor-changed")

        # DBus object initialization
        dbus.service.Object.__init__(
            self,
            pm.dbus_bus_name,
            PGlobs.dbus_objects_basename + "/main"
        )

        try:
            if args.channels is not None:
                for each in args.channels:
                    self.mic_opener.open(each)
        except:
            pass

        try:
            if args.voip is not None:
                if args.voip == ["public"]:
                    self.greenphone.set_active(True)
                elif args.voip == ["private"]:
                    self.redphone.set_active(True)
        except:
            pass

        try:

            if args.kicksources is not None:
                servtabs = self.server_window.streamtabframe.tabs
                for n in range(len(servtabs)):
                    if chr(n + ord("1")) in args.kicksources:
                        servtabs[n].kick_incumbent.clicked()
                time.sleep(0.1)
        except:
            pass

        try:

            if args.servers is not None:
                servtabs = self.server_window.streamtabframe.tabs
                for n in range(len(servtabs)):
                    if chr(n + ord("1")) in args.servers:
                        servtabs[n].server_connect.set_active(True)
                time.sleep(0.1)
                for n in range(len(servtabs)):
                    if chr(n + ord("1")) in args.servers:
                        servtabs[n].server_connect.set_active(False)
                time.sleep(0.1)
                for n in range(len(servtabs)):
                    if chr(n + ord("1")) in args.servers:
                        servtabs[n].server_connect.set_active(True)
        except:
            pass

        try:
            if args.crossfader is not None:
                if args.crossfader == "1":
                    self.passleft.clicked()
                elif args.crossfader == "2":
                    self.passright.clicked()
        except:
            pass

        try:
            if args.players is not None:
                if "1" in args.players:
                    self.player_left.play.clicked()
                if "2" in args.players:
                    self.player_right.play.clicked()
                if "3" in args.players:
                    self.jingles.interlude.play.clicked()
        except:
            pass

    def main(self):
        Gtk.main()


def main():
    try:
        run_instance = MainWindow()
    except (
        MainWindow.initfailed,
        MainWindow.initcleanexit,
            KeyboardInterrupt):
        return 5

    try:
        run_instance.main()
    except KeyboardInterrupt:
        return 5
    except:
        import pdb
        pdb.set_trace()

    return 0
