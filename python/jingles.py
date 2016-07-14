#   jingles.py: Jingles window and players -- part of IDJC.
#   Copyright 2012 Stephen Fairchild (s-fairchild@users.sourceforge.net)
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
import time
import gettext
import json
import uuid

from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import GObject
import urllib.request
import urllib.parse
import urllib.error

from idjc import *
from .playergui import *
from .prelims import *
from .gtkstuff import LEDDict
from .gtkstuff import DefaultEntry
from .gtkstuff import threadslock
from .gtkstuff import timeout_add, source_remove
from .tooltips import set_tip
from .utils import LinkUUIDRegistry

_ = gettext.translation(FGlobs.package_name, FGlobs.localedir,
                        fallback=True).gettext

PM = ProfileManager()
link_uuid_reg = LinkUUIDRegistry()

# Pixbufs for LED's of the specified size.
LED = LEDDict(9)


class Effect(Gtk.Grid):

    """A trigger button for an audio effect or jingle.

    Takes a numeric parameter for identification. Also includes numeric I.D.,
    L.E.D., stop, and config button.
    """

    dndsources = (
        Gtk.TargetEntry.new(
            "IDJC_EFFECT_BUTTON",
            Gtk.TargetFlags.SAME_APP, 6),
    )

    dndtargets = (  # Drag and drop source target specs.
        Gtk.TargetEntry.new('text/plain', 0, 1),
        Gtk.TargetEntry.new('TEXT', 0, 2),
        Gtk.TargetEntry.new('STRING', 0, 3),
        Gtk.TargetEntry.new('text/uri-list', 0, 4),
        Gtk.TargetEntry.new("IDJC_EFFECT_BUTTON", Gtk.TargetFlags.SAME_APP, 6))

    def __init__(self, num, others, parent):
        self.num = num
        self.others = others
        self.approot = parent
        self.pathname = None
        self.uuid = str(uuid.uuid4())
        self._repeat_works = False

        super(Effect, self).__init__()
        self.set_size_request(-1, 1)
        self.set_border_width(2)
        self.set_column_spacing(3)
        self.set_row_baseline_position(0, Gtk.BaselinePosition.CENTER)

        label = Gtk.Label(label="%02d" % (num + 1))
        self.add(label)

        self.clear = LED["clear"].copy()
        self.green = LED["green"].copy()

        self.led = Gtk.Image()
        self.led.set_from_pixbuf(self.clear)
        self.add(self.led)
        self.old_ledval = 0

        image = Gtk.Image.new_from_file(FGlobs.pkgdatadir / "stop.png")
        image.set_padding(4, 4)
        self.stop = Gtk.Button()
        self.stop.set_image(image)
        self.add(self.stop)
        self.stop.connect("clicked", self._on_stop)
        set_tip(self.stop, _('Stop'))

        self.trigger = Gtk.Button()
        self.trigger.set_hexpand(True)
        self.add(self.trigger)
        self.trigger_label = Gtk.Label()
        self.trigger.add(self.trigger_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_orientation(Gtk.Orientation.VERTICAL)
        self.progress.set_inverted(True)
        self.progress.set_size_request(1, 1)
        self.add(self.progress)

        self.trigger.connect("clicked", self._on_trigger)
        self.trigger.drag_dest_set(
            Gtk.DestDefaults.ALL,
            self.dndtargets,
            Gdk.DragAction.DEFAULT | Gdk.DragAction.COPY
        )
        self.trigger.connect("drag-data-received", self._drag_data_received)
        set_tip(self.trigger, _('Play'))

        self.repeat = Gtk.ToggleButton()
        image = Gtk.Image()
        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(
            FGlobs.pkgdatadir / "repeat.png",
            23,
            19
        )
        image.set_from_pixbuf(pb)
        self.repeat.add(image)
        image.show()
        self.add(self.repeat)
        set_tip(self.repeat, _('Repeat'))

        image = Gtk.Image.new_from_stock(
            Gtk.STOCK_PROPERTIES,
            Gtk.IconSize.MENU)
        self.config = Gtk.Button()
        self.config.set_image(image)
        self.add(self.config)
        self.config.connect("clicked", self._on_config)
        self.config.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK,
            self.dndsources, Gdk.DragAction.DEFAULT | Gdk.DragAction.COPY)
        self.config.connect("drag-begin", self._drag_begin)
        self.config.connect("drag-data-get", self._drag_get_data)
        self.config.connect("drag-end", self._drag_end)
        set_tip(self.config, _('Configure'))

        self.dialog = EffectConfigDialog(self, parent.window)
        self.dialog.connect("response", self._on_dialog_response)
        self.dialog.emit("response", Gtk.ResponseType.NO)
        self.timeout_source_id = None
        self.interlude = IDJC_Media_Player(None, None, parent)
        self.effect_length = 0.0

        # Create the widget that will be used in the tab
        self.tabwidget = Gtk.Grid()
        self.tabwidget.set_column_spacing(3)
        self.tabwidget.attach(Gtk.VSeparator(), 0, 0, 1, 2)
        self.tabeffectname = Gtk.Label()
        self.tabeffecttime = Gtk.Label()
        self.tabwidget.add(self.tabeffectname)
        self.tabwidget.add(self.tabeffecttime)
        self.tabeffectprog = Gtk.ProgressBar()
        self.tabeffectprog.set_size_request(-0, 3)
        self.tabwidget.attach_next_to(self.tabeffectprog, self.tabeffectname, Gtk.PositionType.BOTTOM, 2, 1)
        self.tabwidget.show_all()

    def _drag_begin(self, widget, context):
        widget.drag_highlight()
        context.set_icon_stock(Gtk.STOCK_PROPERTIES, -5, -5)

    def _drag_end(self, widget, context):
        widget.drag_unhighlight()

    def _drag_get_data(self, widget, context, selection, target_id, etime):
        selection.set(selection.target, 8, str(self.num))
        return True

    def _drag_data_received(self, widget, context, x, y, dragged, info, etime):
        if context.targets == ["IDJC_EFFECT_BUTTON"]:
            other = self.others[int(dragged.data)]
            if other != self:
                self.stop.clicked()
                other.stop.clicked()
                self._swap(other)
                return True
        else:
            data = dragged.data.splitlines()
            if len(data) == 1 and data[0].startswith("file://"):
                pathname = urllib.parse.unquote(data[0][7:])
                title = self.interlude.get_media_metadata(pathname).title
                if title:
                    self.stop.clicked()
                    self._set(pathname, title, 0.0)
                    return True
        return False

    def _swap(self, other):
        new_pathname = other.pathname
        new_text = other.trigger_label.get_text() or ""
        new_level = other.level

        other._set(
            self.pathname,
            self.trigger_label.get_text() or "",
            self.level
        )
        self._set(new_pathname, new_text, new_level)

    def _set(self, pathname, button_text, level):
        try:
            self.dialog.set_filename(pathname)
        except:
            self.dialog.set_current_folder(os.path.expanduser("~"))

        self.dialog.button_entry.set_text(button_text)
        self.dialog.gain_adj.set_value(level)
        self._on_dialog_response(
            self.dialog,
            Gtk.ResponseType.ACCEPT, pathname
        )

    def _on_config(self, widget):
        self.stop.clicked()
        if self.pathname and os.path.isfile(self.pathname):
            self.dialog.select_filename(self.pathname)
        self.dialog.button_entry.set_text(self.trigger_label.get_text() or "")
        self.dialog.gain_adj.set_value(self.level)
        self.dialog.show()

    def _on_trigger(self, widget):
        self._repeat_works = True
        if self.pathname:
            if not self.timeout_source_id:
                if self.effect_length == 0.0:
                    self.effect_length = self.interlude.get_media_metadata(
                        self.pathname, True
                    )
                self.effect_start = time.time()
                self.timeout_source_id = timeout_add(
                    playergui.PROGRESS_TIMEOUT,
                    self._progress_timeout)
                self.tabeffectname.set_text(self.trigger_label.get_text())
                self.tabeffecttime.set_text('0.0')
                self.tabeffectprog.set_fraction(0.0)
                self.approot.jingles.nb_effects_box.add(
                    self.tabwidget)
                self.approot.effect_started(
                    self.trigger_label.get_text(),
                    self.pathname, self.num)
            else:  # Restarted the effect
                self.effect_start = time.time()

            self.approot.mixer_write(
                "EFCT=%d\nPLRP=%s\nRGDB=%f\nACTN=playeffect\nend\n" % (
                    self.num, self.pathname, self.level))
            self.trigger_label.set_use_markup(True)
            self.trigger_label.set_label(
                "<b>" +
                self.trigger_label.get_text() +
                "</b>")

    def _on_stop(self, widget):
        self._repeat_works = False
        self.approot.mixer_write("EFCT=%d\nACTN=stopeffect\nend\n" % self.num)

    @threadslock
    def _progress_timeout(self):
        now = time.time()
        played = now - self.effect_start
        try:
            ratio = min(played / self.effect_length, 1.0)
        except ZeroDivisionError:
            pass
        else:
            self.progress.set_fraction(ratio)
            self.tabeffectprog.set_fraction(ratio)
            self.tabeffecttime.set_text(
                "%4.1f" % (self.effect_length - played)
            )
        return True

    def _stop_progress(self):
        if self.timeout_source_id:
            source_remove(self.timeout_source_id)
            self.timeout_source_id = None
            self.progress.set_fraction(0.0)
            self.approot.jingles.nb_effects_box.remove(self.tabwidget)
            self.approot.effect_stopped(self.num)

    def _on_dialog_response(self, dialog, response_id, pathname=None):
        if response_id in (Gtk.ResponseType.ACCEPT, Gtk.ResponseType.NO):
            self.pathname = pathname or dialog.get_filename()
            text = dialog.button_entry.get_text() if self.pathname and \
                os.path.isfile(self.pathname) else ""
            self.trigger_label.set_text(text.strip())
            self.level = dialog.gain_adj.get_value()

            if response_id == Gtk.ResponseType.ACCEPT and pathname is not None:
                self.uuid = str(uuid.uuid4())
            self.effect_length = 0.0  # Force effect length to be read again.

    def marshall(self):
        link = link_uuid_reg.get_link_filename(self.uuid)
        if link is not None:
            # Replace orig file abspath with alternate path to a hard link
            # except when link is None as happens when a hard link fails.
            link = PathStr("links") / link
            self.pathname = PM.basedir / link
            if not self.dialog.get_visible():
                self.dialog.set_filename(self.pathname)
        return json.dumps([
            self.trigger_label.get_text(),
            (link or self.pathname),
            self.level, self.uuid
        ])

    def unmarshall(self, data):
        try:
            label, pathname, level, self.uuid = json.loads(data)
        except ValueError:
            label = ""
            pathname = None
            level = 0.0

        if pathname is not None and not pathname.startswith(os.path.sep):
            pathname = PM.basedir / pathname
        if pathname is None or not os.path.isfile(pathname):
            self.dialog.unselect_all()
            label = ""
        else:
            self.dialog.set_filename(pathname)
        self.dialog.button_entry.set_text(label)
        self.dialog.gain_adj.set_value(level)
        self._on_dialog_response(
            self.dialog,
            Gtk.ResponseType.ACCEPT,
            pathname
        )
        self.pathname = pathname

    def update_led(self, val):
        if val != self.old_ledval:
            self.led.set_from_pixbuf(self.green if val else self.clear)
            self.old_ledval = val

            if not val and self._repeat_works and self.repeat.get_active():
                self.trigger.clicked()
            elif not val:
                self._stop_progress()


class EffectConfigDialog(Gtk.FileChooserDialog):

    """Configuration dialog for an Effect."""

    file_filter = Gtk.FileFilter()
    file_filter.set_name(_('Supported media'))
    for each in supported.media:
        if each not in (".cue", ".txt"):
            file_filter.add_pattern("*" + each)
            file_filter.add_pattern("*" + each.upper())

    def __init__(self, effect, window):
        super(EffectConfigDialog, self).__init__()
        self.set_title(_('Effect %d Config') % (effect.num + 1))
        self.set_parent(window)
        self.add_buttons(Gtk.STOCK_CLEAR, Gtk.ResponseType.NO,
                         Gtk.STOCK_CANCEL, Gtk.ResponseType.REJECT,
                         Gtk.STOCK_OK, Gtk.ResponseType.ACCEPT)
        self.set_modal(True)

        ca = self.get_content_area()
        ca.set_spacing(5)
        grid = Gtk.Grid()
        ca.pack_start(grid, False, False, 0)
        grid.set_border_width(5)

        grid.set_column_spacing(3)
        label = Gtk.Label(label=_('Trigger text'))
        self.button_entry = DefaultEntry(_('No Name'))
        grid.add(label)
        grid.add(self.button_entry)


        label = Gtk.Label(label=_('Level adjustment (dB)'))
        self.gain_adj = Gtk.Adjustment(0.0, -10.0, 10.0, 0.5)
        gain = Gtk.SpinButton.new(self.gain_adj, 1.0, 1)
        grid.attach(label, 3, 0, 1, 1)
        grid.add(gain)


        ca.show_all()
        self.connect("notify::visible", self._cb_notify_visible)
        self.connect("delete-event", lambda w, e: w.hide() or True)
        self.connect("response", self._cb_response)
        self.add_filter(self.file_filter)

    def set_filename(self, filename):
        self._stored_filename = filename
        Gtk.FileChooserDialog.set_filename(self, filename)

    def _cb_notify_visible(self, *args):
        # Make sure filename is shown in the location box.

        if self.get_visible():
            filename = self.get_filename()
            if filename is None:
                try:
                    if self._stored_filename is not None:
                        self.set_filename(self._stored_filename)
                except AttributeError:
                    pass
        else:
            self._stored_filename = self.get_filename()

    def _cb_response(self, dialog, response_id):
        dialog.hide()
        if response_id == Gtk.ResponseType.NO:
            dialog.unselect_all()
            dialog.set_current_folder(os.path.expanduser("~"))
            self.button_entry.set_text("")
            self.gain_adj.set_value(0.0)


class EffectBank(Gtk.Frame):

    """A vertical stack of effects with level controls."""

    def __init__(self, qty, base, filename, parent, all_effects, vol_adj, mute_adj):
        super(EffectBank, self).__init__()
        self.base = base
        self.session_filename = filename

        grid = Gtk.Grid()
        grid.set_column_spacing(1)
        self.add(grid)

        self.effects = []
        self.all_effects = all_effects

        p = Gtk.ProgressBar()
        p.set_orientation(Gtk.Orientation.VERTICAL)
        for row in range(qty):
            effect = Effect(base + row, self.all_effects, parent)
            self.effects.append(effect)
            self.all_effects.append(effect)
            grid.attach(effect, 0, row, 1, 1)

        vol_image = Gtk.Image.new_from_file(FGlobs.pkgdatadir / "volume2.png")
        vol = Gtk.VScale(adjustment=vol_adj)
        vol.set_inverted(True)
        vol.set_draw_value(False)
        vol.set_vexpand(True)
        set_tip(vol, _('Effects volume.'))

        pb = GdkPixbuf.Pixbuf.new_from_file(FGlobs.pkgdatadir / "headroom.png")
        mute_image = Gtk.Image.new_from_pixbuf(pb)
        mute = Gtk.VScale(adjustment=mute_adj)
        mute.set_inverted(True)
        mute.set_vexpand(True)
        mute.set_draw_value(False)
        set_tip(
            mute,
            _('Player headroom that is applied when an effect is playing.')
        )

        volumes_grid = Gtk.Grid()
        volumes_grid.set_orientation(Gtk.Orientation.VERTICAL)
        row = 0
        for row, widget in enumerate((vol_image, vol, mute_image, mute)):
            volumes_grid.add(widget)
        grid.attach(volumes_grid, 1, 0, 1, qty)

    def marshall(self):
        return json.dumps([x.marshall() for x in self.effects])

    def unmarshall(self, data):
        for per_widget_data, widget in zip(json.loads(data), self.effects):
            widget.unmarshall(per_widget_data)

    def restore_session(self):
        try:
            with open(PM.basedir / self.session_filename, "r") as f:
                self.unmarshall(f.read())
        except IOError:
            print("failed to read effects session file")

    def save_session(self, where):
        try:
            with open((where or PM.basedir) / self.session_filename, "w") as f:
                f.write(self.marshall())
        except IOError:
            print("failed to write effects session file")

    def update_leds(self, bits):
        for bit, each in enumerate(self.effects):
            each.update_led((1 << bit + self.base) & bits)

    def stop(self):
        for each in self.effects:
            each.stop.clicked()

    def uuids(self):
        return (x.uuid for x in self.widgets)

    def pathnames(self):
        return (x.pathname for x in self.widgets)


class ExtraPlayers(Gtk.Grid):

    """For effects, and background tracks."""

    def __init__(self, parent):
        self.approot = parent

        super(ExtraPlayers, self).__init__()

        # Tab label
        self.nb_label = Gtk.Grid()
        lbl = Gtk.Label(label=_('Effects'))
        lbl.set_padding(0, 2)
        self.nb_label.add(lbl)

        self.nb_effects_box = Gtk.Grid()
        self.nb_label.add(self.nb_effects_box)
        self.nb_label.show_all()
        self.nb_effects_box.hide()
        self.set_border_width(4)
        self.set_column_spacing(10)
        self.viewlevels = (5,)

        # Tab content
        estable = Gtk.Table(columns=2, homogeneous=True)
        self.add(estable)
        estable.set_col_spacing(1, 8)

        self.jvol_adj = (Gtk.Adjustment(127.0, 0.0, 127.0, 1.0, 10.0),
                         Gtk.Adjustment(127.0, 0.0, 127.0, 1.0, 10.0))
        self.jmute_adj = (Gtk.Adjustment(100.0, 0.0, 127.0, 1.0, 10.0),
                          Gtk.Adjustment(100.0, 0.0, 127.0, 1.0, 10.0))
        self.ivol_adj = Gtk.Adjustment(64.0, 0.0, 127.0, 1.0, 10.0)
        for each in (self.jvol_adj[0], self.jvol_adj[1], self.ivol_adj,
                     self.jmute_adj[0], self.jmute_adj[1]):
            each.connect("value-changed",
                         lambda w: parent.send_new_mixer_stats())

        effects_grid = Gtk.Grid()
        effects_grid.set_column_spacing(6)
        effects = PGlobs.num_effects
        base = 0
        max_rows = 12
        effect_cols = (effects + max_rows - 1) // max_rows
        self.all_effects = []
        self.effect_banks = []
        for col in range(effect_cols):
            bank = EffectBank(
                min(effects - base, max_rows), base,
                "effects%d_session" % (col + 1), parent, self.all_effects,
                self.jvol_adj[col], self.jmute_adj[col])
            parent.label_subst.add_widget(
                bank,
                "effectbank%d" % col, _('Effects %d') % (col + 1))
            self.effect_banks.append(bank)
            effects_grid.attach(bank, col, 0, 1, 1)
            base += max_rows
        estable.attach(effects_grid, 0, 2, 0, 1)

        self.interlude_frame = interlude_frame = Gtk.Frame()
        parent.label_subst.add_widget(interlude_frame, "bgplayername",
                                      _('Background Tracks'))
        self.add(interlude_frame)
        framegrid = Gtk.Grid()
        framegrid.set_column_spacing(1)
        interlude_box = Gtk.VBox() # TODO: use grid after edit IDJC_Media_Player
        interlude_frame.add(framegrid)
        framegrid.add(interlude_box)
        self.interlude = IDJC_Media_Player(interlude_box, "interlude", parent)
        interlude_box.set_vexpand(True)
        interlude_box.set_no_show_all(True)

        ilevel = Gtk.Grid()
        ilevel.set_orientation(Gtk.Orientation.VERTICAL)
        framegrid.add(ilevel)
        volpb = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "volume2.png"
        )
        ivol_image = Gtk.Image.new_from_pixbuf(volpb)
        ilevel.add(ivol_image)
        ivol = Gtk.VScale(adjustment=self.ivol_adj)
        ivol.set_inverted(True)
        ivol.set_vexpand(True)
        ivol.set_draw_value(False)
        ilevel.add(ivol)
        set_tip(ivol, _('Background Tracks volume.'))

        self.show_all()
        interlude_box.show()
        self.approot.player_nb.connect('switch-page',
                                       self._on_nb_switch_page,
                                       self.nb_effects_box)

    def _on_nb_switch_page(self, notebook, page, page_num, box):
        page_widget = notebook.get_nth_page(page_num)
        if isinstance(page_widget, ExtraPlayers):
            box.hide()
        else:
            box.show()

    def restore_session(self):
        for each in self.effect_banks:
            each.restore_session()
        self.interlude.restore_session()

    def save_session(self, where):
        for each in self.effect_banks:
            each.save_session(where)
        self.interlude.save_session(where)

    def update_effect_leds(self, ep):
        for each in self.effect_banks:
            each.update_leds(ep)

    def clear_indicators(self):
        """Set all LED indicators to off."""

        pass

    def cleanup(self):
        pass

    @property
    def playing(self):
        return False

    @property
    def flush(self):
        return 0

    @flush.setter
    def flush(self, value):
        pass

    @property
    def interludeflush(self):
        return 0

    @interludeflush.setter
    def interludeflush(self, value):
        pass
