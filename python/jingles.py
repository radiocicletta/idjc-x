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

from __future__ import print_function

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


class Effect(Gtk.HBox):

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
        self.set_spacing(3)
        self.set_homogeneous(False)
        self.set_baseline_position(Gtk.BaselinePosition.CENTER)

        label = Gtk.Label(label="%02d" % (num + 1))
        self.pack_start(label, False, False, 0)

        self.clear = LED["clear"].copy()
        self.green = LED["green"].copy()

        self.led = Gtk.Image()
        self.led.set_from_pixbuf(self.clear)
        self.pack_start(self.led, False, False, 0)
        self.old_ledval = 0

        image = Gtk.Image.new_from_file(FGlobs.pkgdatadir / "stop.png")
        image.set_padding(4, 4)
        self.stop = Gtk.Button()
        self.stop.set_image(image)
        self.pack_start(self.stop, False, False, 0)
        self.stop.connect("clicked", self._on_stop)
        set_tip(self.stop, _('Stop'))

        self.trigger = Gtk.Button()
        self.trigger.set_size_request(80, -1)
        self.pack_start(self.trigger, True, True, 0)
        self.trigger_label = Gtk.Label()
        self.trigger.add(self.trigger_label)

        pvbox = Gtk.VBox()
        self.progress = Gtk.ProgressBar()
        self.progress.set_orientation(Gtk.Orientation.VERTICAL)
        self.progress.set_inverted(True)
        self.progress.set_size_request(1, 1)
        pvbox.pack_start(self.progress, True, True, 1)
        self.pack_start(pvbox, False, False, 0)
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
        self.pack_start(self.repeat, False, False, 0)
        set_tip(self.repeat, _('Repeat'))

        image = Gtk.Image.new_from_stock(
            Gtk.STOCK_PROPERTIES,
            Gtk.IconSize.MENU)
        self.config = Gtk.Button()
        self.config.set_image(image)
        self.pack_start(self.config, False, False, 0)
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
        self.tabwidget = Gtk.HBox()
        self.tabwidget.set_spacing(3)
        sep = Gtk.VSeparator()
        self.tabwidget.pack_start(sep, True, True, 0)
        vb = Gtk.VBox()
        self.tabwidget.pack_start(vb, True, True, 0)
        hb = Gtk.HBox()
        hb.set_spacing(3)
        self.tabeffectname = Gtk.Label()
        self.tabeffecttime = Gtk.Label()
        hb.pack_start(self.tabeffectname, True, True, 0)
        hb.pack_start(self.tabeffecttime, True, True, 0)
        vb.pack_start(hb, True, True, 0)
        self.tabeffectprog = Gtk.ProgressBar()
        self.tabeffectprog.set_size_request(-0, 3)
        vb.pack_start(self.tabeffectprog, True, True, 0)
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
                self.approot.jingles.nb_effects_box.pack_start(
                    self.tabwidget, True, True, 0)
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
        GObject.GObject.__init__(self)
        self.set_title(_('Effect %d Config') % (effect.num + 1))
        self.set_parent(window)
        self.add_buttons(Gtk.STOCK_CLEAR, Gtk.ResponseType.NO,
                         Gtk.STOCK_CANCEL, Gtk.ResponseType.REJECT,
                         Gtk.STOCK_OK, Gtk.ResponseType.ACCEPT)
        self.set_modal(True)

        ca = self.get_content_area()
        ca.set_spacing(5)
        vbox = Gtk.VBox()
        ca.pack_start(vbox, False, False, 0)
        vbox.set_border_width(5)

        hbox = Gtk.HBox()
        hbox.set_spacing(3)
        label = Gtk.Label(label=_('Trigger text'))
        self.button_entry = DefaultEntry(_('No Name'))
        hbox.pack_start(label, False, False, 0)
        hbox.pack_start(self.button_entry, False, False, 0)

        spc = Gtk.HBox()
        hbox.pack_start(spc, False, False, 3)

        label = Gtk.Label(label=_('Level adjustment (dB)'))
        self.gain_adj = Gtk.Adjustment(0.0, -10.0, 10.0, 0.5)
        gain = Gtk.SpinButton.new(self.gain_adj, 1.0, 1)
        hbox.pack_start(label, False, False, 0)
        hbox.pack_start(gain, False, False, 0)

        vbox.pack_start(hbox, False, False, 0)

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
        GObject.GObject.__init__(self)
        self.base = base
        self.session_filename = filename

        hbox = Gtk.HBox()
        hbox.set_spacing(1)
        self.add(hbox)
        vbox = Gtk.VBox()
        hbox.pack_start(vbox, True, True, 0)

        self.effects = []
        self.all_effects = all_effects

        p = Gtk.ProgressBar()
        p.set_orientation(Gtk.Orientation.VERTICAL)
        for row in range(qty):
            effect = Effect(base + row, self.all_effects, parent)
            self.effects.append(effect)
            self.all_effects.append(effect)
            vbox.pack_start(effect, True, True, 0)

        level_vbox = Gtk.VBox()
        hbox.pack_start(level_vbox, False, False, 3)

        vol_image = Gtk.Image.new_from_file(FGlobs.pkgdatadir / "volume2.png")
        vol = Gtk.VScale(adjustment=vol_adj)
        vol.set_inverted(True)
        vol.set_draw_value(False)
        set_tip(vol, _('Effects volume.'))

        pb = GdkPixbuf.Pixbuf.new_from_file(FGlobs.pkgdatadir / "headroom.png")
        mute_image = Gtk.Image.new_from_pixbuf(pb)
        mute = Gtk.VScale(adjustment=mute_adj)
        mute.set_inverted(True)
        mute.set_draw_value(False)
        set_tip(
            mute,
            _('Player headroom that is applied when an effect is playing.')
        )

        spc = Gtk.VBox()

        for widget, expand in zip((vol_image, vol, spc, mute_image, mute),
                                  (False, True, False, False, True)):
            level_vbox.pack_start(widget, expand, True, 2)

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


class LabelSubst(Gtk.Frame):

    def __init__(self, heading):
        GObject.GObject.__init__(self, " %s " % heading)
        self.vbox = Gtk.VBox()
        self.vbox.set_border_width(2)
        self.vbox.set_spacing(2)
        self.add(self.vbox)
        self.textdict = {}
        self.activedict = {}

    def add_widget(self, widget, ui_name, default_text):
        frame = Gtk.Frame(" %s " % default_text)
        frame.set_label_align(0.5, 0.5)
        frame.set_border_width(3)
        self.vbox.pack_start(frame, True, True, 0)
        hbox = Gtk.HBox()
        hbox.set_spacing(3)
        frame.add(hbox)
        hbox.set_border_width(2)
        use_supplied = Gtk.RadioButton(None, label=_("Alternative"))
        use_default = Gtk.RadioButton(None, label=_('Default'))
        self.activedict[ui_name + "_use_supplied"] = use_supplied
        hbox.pack_start(use_default, False)
        hbox.pack_start(use_supplied, False)
        entry = Gtk.Entry()
        self.textdict[ui_name + "_text"] = entry
        hbox.pack_start(entry, True, True, 0)

        if isinstance(widget, Gtk.Frame):
            def set_text(new_text):
                new_text = new_text.strip()
                if new_text:
                    new_text = " %s " % new_text
                widget.set_label(new_text or None)
            widget.set_text = set_text

        entry.connect("changed", self.cb_entry_changed, widget, use_supplied)
        args = default_text, entry, widget
        use_default.connect("toggled", self.cb_radio_default, *args)
        use_supplied.connect_object("toggled", self.cb_radio_default,
                                    use_default, *args)
        use_default.set_active(True)

    def cb_entry_changed(self, entry, widget, use_supplied):
        if use_supplied.get_active():
            widget.set_text(entry.get_text())
        elif entry.has_focus():
            use_supplied.set_active(True)

    def cb_radio_default(self, use_default, default_text, entry, widget):
        if use_default.get_active():
            widget.set_text(default_text)
        else:
            widget.set_text(entry.get_text())
            entry.grab_focus()


class ExtraPlayers(Gtk.HBox):

    """For effects, and background tracks."""

    def __init__(self, parent):
        self.approot = parent

        self.nb_label = Gtk.HBox(False, 0)
        vb = Gtk.VBox()
        lbl = Gtk.Label(label=_('Effects'))
        lbl.set_padding(0, 2)
        vb.pack_start(lbl, True, True, 0)
        vb.show()
        self.nb_label.pack_start(vb, True, True, 0)
        self.nb_effects_box = Gtk.HBox(False, 5)
        self.nb_label.pack_start(self.nb_effects_box, True, True, 0)
        self.nb_label.show_all()
        self.nb_effects_box.hide()
        GObject.GObject.__init__(self)
        self.set_border_width(4)
        self.set_spacing(10)
        self.viewlevels = (5,)

        esbox = Gtk.VBox()
        self.pack_start(esbox, True, True, 0)
        estable = Gtk.Table(columns=2, homogeneous=True)
        estable.set_col_spacing(1, 8)
        esbox.pack_start(estable, True, True, 0)

        self.jvol_adj = (Gtk.Adjustment(127.0, 0.0, 127.0, 1.0, 10.0),
                         Gtk.Adjustment(127.0, 0.0, 127.0, 1.0, 10.0))
        self.jmute_adj = (Gtk.Adjustment(100.0, 0.0, 127.0, 1.0, 10.0),
                          Gtk.Adjustment(100.0, 0.0, 127.0, 1.0, 10.0))
        self.ivol_adj = Gtk.Adjustment(64.0, 0.0, 127.0, 1.0, 10.0)
        for each in (self.jvol_adj[0], self.jvol_adj[1], self.ivol_adj,
                     self.jmute_adj[0], self.jmute_adj[1]):
            each.connect("value-changed",
                         lambda w: parent.send_new_mixer_stats())

        effects_hbox = Gtk.HBox(homogeneous=True)
        effects_hbox.set_spacing(6)
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
            effects_hbox.pack_start(bank, True, True, 0)
            base += max_rows
        estable.attach(effects_hbox, 0, 2, 0, 1)

        self.interlude_frame = interlude_frame = Gtk.Frame()
        parent.label_subst.add_widget(interlude_frame, "bgplayername",
                                      _('Background Tracks'))
        self.pack_start(interlude_frame, True, True, 0)
        hbox = Gtk.HBox()
        hbox.set_spacing(1)
        interlude_frame.add(hbox)
        interlude_box = Gtk.VBox()
        hbox.pack_start(interlude_box, True, True, 0)
        self.interlude = IDJC_Media_Player(interlude_box, "interlude", parent)
        interlude_box.set_no_show_all(True)

        ilevel_vbox = Gtk.VBox()
        hbox.pack_start(ilevel_vbox, False, False, 3)
        volpb = GdkPixbuf.Pixbuf.new_from_file(
            FGlobs.pkgdatadir / "volume2.png"
        )
        ivol_image = Gtk.Image.new_from_pixbuf(volpb)
        ilevel_vbox.pack_start(ivol_image, False, False, 2)
        ivol = Gtk.VScale(adjustment=self.ivol_adj)
        ivol.set_inverted(True)
        ivol.set_draw_value(False)
        ilevel_vbox.pack_start(ivol, True, True, 2)
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
