#   IDJCmixprefs.py: Preferences window code for IDJC
#   Copyright (C) 2005-2011 Stephen Fairchild (s-fairchild@users.sourceforge.net)
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
import shutil
import gettext
import itertools

from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import GLib

from idjc import FGlobs, PGlobs
from . import licence_window
from . import songdb
from . import midicontrols
from .gtkstuff import WindowSizeTracker, DefaultEntry, threadslock
from .gtkstuff import timeout_add, source_remove
from .prelims import ProfileManager
from .utils import PathStr
from .tooltips import set_tip, MAIN_TIPS

__all__ = ['mixprefs', 'PanPresetChooser']


_ = gettext.translation(
    FGlobs.package_name,
    FGlobs.localedir,
    fallback=True).gettext


def N_(text):
    return text


pm = ProfileManager()


class CSLEntry(Gtk.Entry):

    def cb_keypress(self, widget, event):
        if event.string:
            if len(event.string) > 1:
                return True
            if not event.string in "0123456789,":
                return True
        return False

    def __init__(self, max=0):
        super(CSLEntry, self).__init__(max)
        self.connect("key-press-event", self.cb_keypress)


class InitialPlayerConfig(Gtk.Frame):

    def __init__(self, title, player, prefix):
        self.player = player
        super(InitialPlayerConfig, self).__init__()
        self.set_label(" %s " % title)
        vbox = Gtk.VBox()
        vbox.set_border_width(3)
        self.add(vbox)

        pl_label = Gtk.Label(label=_("Playlist Mode"))
        fade_label = Gtk.Label(label=_("Fade"))
        self.pl_mode = Gtk.ComboBoxText()
        self.pl_mode.set_model(player.pl_mode.get_model())
        self.fade = Gtk.ComboBoxText()
        self.fade.set_model(player.pl_delay.get_model())

        for each in (self.pl_mode, self.fade):
            each.set_active(0)

        self.elapsed = Gtk.RadioButton(None, label=_("Track time elapsed"))
        self.remaining = Gtk.RadioButton(
            self.elapsed,
            label=_("Track time remaining"))
        self.remaining.join_group(self.elapsed)
        s1 = Gtk.HSeparator()
        self.to_stream = Gtk.CheckButton(_("Audio to stream"))
        self.to_dj = Gtk.CheckButton(_("Audio to DJ"))

        for each in (self.to_stream, self.to_dj):
            each.set_active(True)

        for each in (pl_label, self.pl_mode, fade_label, self.fade,
                     self.elapsed, self.remaining, s1, self.to_stream, self.to_dj):
            vbox.pack_start(each, False, False, 0)
        self.show_all()

        self.activedict = {
            prefix + "pl_mode": self.pl_mode,
            prefix + "fade": self.fade,
            prefix + "timeremaining": self.remaining,
            prefix + "tostream": self.to_stream,
            prefix + "todj": self.to_dj
        }

    def apply(self):
        p = self.player

        p.pl_mode.set_active(self.pl_mode.get_active())
        p.pl_delay.set_active(self.fade.get_active())
        p.stream.set_active(self.to_stream.get_active())
        p.listen.set_active(self.to_dj.get_active())
        if self.remaining.get_active():
            p.digiprogress_click()


class PanWidget(Gtk.Frame):
    _instances = []

    def __init__(self, title, commandname):
        self._instances.append(self)

        super(PanWidget, self).__init__()
        self.modes = (1, 2, 3)
        set_tip(
            self,
            _('Stereo panning is the selection of where an audio '
              'source sits from left to right within the stereo mix.\n\n'
              'This control maintains constant audio power '
              'throughout its range of travel, giving -3dB attenuation in '
              'both audio channels at the half way point.\n\n'
              'If you require 0dB straight down the middle or '
              'require a stereo source remain as stereo then '
              'this feature should be turned off.\n\n'
              'Paired channels should be set to 100% left/right unless narrowing of '
              'the stereo field is the intention.'))
        self.valuesdict = {}
        self.activedict = {}
        self._source_id = None

        hbox = Gtk.HBox()
        self.pan_active = Gtk.CheckButton(title)
        self.activedict[commandname + "_pan_active"] = self.pan_active
        hbox.pack_start(self.pan_active, False, False, 0)
        self.pan_active.show()
        self.set_label_widget(hbox)
        hbox.show()

        panvbox = Gtk.VBox()
        panvbox.set_border_width(1)
        self.add(panvbox)

        panadj = Gtk.Adjustment(
            value=50.0,
            lower=0.0,
            upper=100.0,
            step_increment=1,
            page_increment=1,
            page_size=0)
        self.pan = Gtk.HScale(adjustment=panadj)
        self.pan.set_digits(0)
        self.pan.connect("format-value", self._cb_format_value)
        self.valuesdict[commandname + "_pan"] = self.pan
        panvbox.pack_start(self.pan, False, False, 2)

        label = Gtk.Label(label=_('Presets'))
        label.set_alignment(0.0, 0.5)
        label.set_padding(3, 3)
        panvbox.pack_start(label, False, False, 0)

        self._presets = []
        for i in range(PGlobs.num_panpresets):
            preadj = Gtk.Adjustment(
                value=50.0,
                lower=0.0,
                upper=100.0,
                step_increment=1,
                page_increment=1,
                page_size=0)
            preset = Gtk.HScale(adjustment=preadj)
            preset.set_digits(0)
            preset.connect("format-value", self._cb_format_value)
            self.valuesdict[commandname + "_panpreset" + str(i)] = preset
            self._presets.append(preset)
            panvbox.pack_start(preset, False, False, 2)

    def load_preset(self, index):
        try:
            target = int(self._presets[index].get_value() + 0.5)
        except IndexError:
            print("Attempt made to load a non existent pan preset")
        else:
            if self._source_id:
                source_remove(self._source_id)

            self._source_id = timeout_add(5, self._timeout, target)

    @threadslock
    def _timeout(self, target):
        current_value = int(self.pan.get_value() + 0.5)
        new_value = current_value + cmp(target, current_value)
        self.pan.set_value(new_value)

        return new_value != target

    @classmethod
    def load_presets(cls, index):
        for self in cls._instances:
            self.load_preset(index)

    def set_values(self, value):
        self.pan.set_value(value)
        for each in self._presets:
            each.set_value(value)

    def _cb_format_value(self, scale, value):
        if value == 50:
            return "\u25C8"

        pc = str(abs(int(value) * 2 - 100))
        if value < 50:
            return "\u25C4 %s%%" % pc

        return "%s%% \u25BA" % pc


class PanPresetButton(Gtk.Button):

    def __init__(self, labeltext):
        self._labeltext = labeltext
        super(PanPresetButton, self).__init__()
        self._label = Gtk.Label(label=labeltext)
        self.add(self._label)

    def highlight(self):
        self._label.set_markup(
            "<span foreground='red'>%s</span>" %
            self._labeltext)

    def unhighlight(self):
        self._label.set_text(self._labeltext)


class PanPresetChooser(Gtk.HBox):

    def __init__(self):
        super(PanPresetChooser, self).__init__()
        self.set_spacing(1)

        label = Gtk.Label(label="\u25C4")
        self.pack_start(label, True, True, 0)
        label.show()

        self.buttons = []
        for i in range(PGlobs.num_panpresets):
            button = PanPresetButton(str(i + 1))
            self.pack_start(button, False, False, 0)
            button.show()
            self.buttons.append(button)
            button.connect_object("clicked", PanWidget.load_presets, i)
            button.connect("clicked", self._cb_clicked)

        label = Gtk.Label(label="\u25BA")
        self.pack_start(label, True, True, 0)
        label.show()

        set_tip(
            self,
            _('The pan preset selection buttons.\n\n'
              'In the stereo image at a click the DJ '
              'can be on the left and a guest '
              'on the right and when the guest is '
              'gone at a click the DJ can be '
              'central again.\n\n'
              'Note: preconfiguration of pan '
              'preset settings is required.'))

    def load_preset(self, index):
        try:
            button = self.buttons[index]
        except IndexError:
            pass
        else:
            button.clicked()

    def _cb_clicked(self, clicked_button):
        for button in self.buttons:
            if button is clicked_button:
                button.highlight()
            else:
                button.unhighlight()


class AGCControl(Gtk.Frame, Gtk.Activatable):
    mic_modes = (
        # TC: Microphone mode combobox text.
        N_('Deactivated'),
        # TC: Microphone mode combobox text.
        N_('Basic input'),
        # TC: Microphone mode combobox text.
        N_('Processed input'),
        # TC: Microphone mode combobox text.
        N_('Partnered with channel %s'))

    def sendnewstats(self, widget, wname):
        if wname != NotImplemented:
            if isinstance(widget, (Gtk.SpinButton, Gtk.Scale)):
                value = widget.get_value()
            if isinstance(widget, (Gtk.ToggleButton, Gtk.ComboBox)):
                value = int(widget.get_active())
            stringtosend = "INDX=%d\nAGCP=%s=%s\nACTN=%s\nend\n" % (
                self.index, wname, str(value), "mic_control")
            self.approot.mixer_write(stringtosend)

    def set_partner(self, partner):
        self.partner = partner
        self.mode.set_cell_data_func(self.mode_cell,
                                     self.mode_cell_data_func, partner.mode)

    def mode_cell_data_func(self, celllayout, cell, model, iter, opposite):
        index = model.get_path(iter)[0]
        oindex = opposite.get_active()
        cell.props.sensitive = not ((
            (index == 0 or index == 3) and
            oindex == 3) or
            (index == 3 and oindex == 0))
        trans = _(model.get_value(iter, 0))
        if index == 3:
            cell.props.text = trans % self.partner.ui_name
        else:
            cell.props.text = trans

    def numline(self, label_text, wname, initial=0, mini=0, maxi=0, step=0,
                digits=0, adj=None):
        hbox = Gtk.HBox()
        label = Gtk.Label(label=label_text)
        if not adj:
            adj = Gtk.Adjustment(value=initial, lower=mini,
                                 upper=maxi, step_increment=step)
        sb = Gtk.SpinButton(adjustment=adj, climb_rate=0, digits=digits)
        sb.connect("value-changed", self.sendnewstats, wname)
        sb.emit("value-changed")
        hbox.pack_start(label, False, False, 0)
        hbox.pack_end(sb, False, False, 0)
        hbox.show_all()
        self.valuesdict[self.commandname + "_" + wname] = sb
        self.fixups.append(lambda: sb.emit("value-changed"))
        return hbox

    def frame(self, label, container):
        frame = Gtk.Frame(label=label)
        container.pack_start(frame, False, False, 0)
        frame.show()
        ivbox = Gtk.VBox()
        ivbox.set_border_width(3)
        frame.add(ivbox)
        ivbox.show()
        return ivbox

    def widget_frame(self, widget, container, tip, modes):
        frame = Gtk.Frame()
        frame.modes = modes
        set_tip(frame, tip)
        frame.set_label_widget(widget)
        container.pack_start(frame, False, False, 0)
        frame.show()
        ivbox = Gtk.VBox()
        ivbox.set_border_width(3)
        frame.add(ivbox)
        ivbox.show()
        return ivbox

    def toggle_frame(self, label_text, wname, container):
        frame = Gtk.Frame()
        cb = Gtk.CheckButton(label_text)
        cb.connect("toggled", self.sendnewstats, wname)
        cb.emit("toggled")
        cbb = Gtk.HBox()
        cbb.pack_start(cb, True, False, 2)
        cb.show()
        frame.set_label_widget(cbb)
        cbb.show()
        container.pack_start(frame, False, False, 0)
        frame.show()
        ivbox = Gtk.VBox()
        ivbox.set_border_width(3)
        frame.add(ivbox)
        ivbox.show()
        self.activedict[self.commandname + "_" + wname] = cb
        self.fixups.append(lambda: cb.emit("toggled"))
        return ivbox

    def check(self, label_text, wname, save=True):
        cb = Gtk.CheckButton(label_text)
        cb.connect("toggled", self.sendnewstats, wname)
        cb.emit("toggled")
        cb.show()
        if save:
            self.activedict[self.commandname + "_" + wname] = cb
        self.fixups.append(lambda: cb.emit("toggled"))
        return cb

    def cb_open(self, widget):
        active = widget.get_active()
        self.meter.set_led(active)
        if Gtk.main_level():
            self.approot.channelstate_changed(self.index, active)

    def cb_mode(self, combobox):
        mode = combobox.get_active()

        # Show pertinent features for each mode.
        def showhide(widget):
            try:
                modes = widget.modes
            except:
                pass
            else:
                if mode in modes:
                    widget.show()
                else:
                    widget.hide()
        self.vbox.foreach(showhide)

        # Meter sensitivity. Deactivated => insensitive.
        sens = mode != 0
        self.meter.set_sensitive(sens)
        if not sens:
            self.open.set_active(False)
        if self.partner:
            if mode == 3:
                self.open.set_related_action(self.partner.openaction)
            else:
                self.open.set_related_action(self.partner.openaction)
                self.open.set_sensitive(
                    self.no_front_panel_opener.get_active())

    def __init__(self, approot, ui_name, commandname, index):
        self.approot = approot
        self.partner = None
        self.ui_name = ui_name
        self.meter = approot.mic_meters[int(ui_name) - 1]
        self.meter.agc = self
        self.commandname = commandname
        self.index = index
        self.valuesdict = {}
        self.activedict = {}
        self.textdict = {}
        self.fixups = []
        super(AGCControl, self).__init__()
        hbox = Gtk.HBox()
        hbox.set_spacing(3)

        label = Gtk.Label(label='<span weight="600">' + ui_name + "</span>")
        label.set_use_markup(True)
        hbox.pack_start(label, False, False, 0)
        label.show()

        self.alt_name = Gtk.Entry()
        set_tip(self.alt_name, _('A label so you may describe briefly the '
                                 'role of this audio channel.'))
        self.textdict[self.commandname + "_alt_name"] = self.alt_name
        hbox.pack_start(self.alt_name, True, True, 0)
        self.alt_name.show()
        hbox.show()

        self.set_label_widget(hbox)
        hbox.show()
        self.set_label_align(0.5, 0.5)
        self.set_border_width(3)
        self.vbox = Gtk.VBox()
        self.vbox.set_spacing(2)
        self.vbox.set_border_width(3)
        self.add(self.vbox)
        self.vbox.show()

        mode_liststore = Gtk.ListStore(str)
        self.mode = Gtk.ComboBox()
        self.mode.set_model(mode_liststore)
        self.mode_cell = Gtk.CellRendererText()
        self.mode.pack_start(self.mode_cell, True)
        #self.mode.set_attributes(self.mode_cell, text=0)
        self.fixups.append(lambda: self.mode.emit("changed"))

        self.vbox.pack_start(self.mode, False, False, 0)
        self.open = Gtk.ToggleButton()

        for each in self.mic_modes:
            mode_liststore.append((each, ))
        self.mode.connect("changed", self.sendnewstats, "mode")
        self.mode.connect("changed", self.cb_mode)
        self.activedict[self.commandname + "_mode"] = self.mode
        self.mode.show()
        set_tip(self.mode, _('The signal processing mode.'))

        hbox = Gtk.HBox()
        # TC: A frame heading. The channel opener is selected within.
        label = Gtk.Label(label=_('Channel Opener'))
        hbox.pack_start(label, False, False, 3)
        label.show()
        ivbox = self.widget_frame(
            hbox,
            self.vbox,
            _('This controls the allocation of front panel '
              'open/unmute buttons. Having one button '
              'control multiple microphones can save time.'),
            (1, 2)
        )
        hbox.show()

        hbox = Gtk.HBox()
        # TC: Spinbutton label text.
        self.group = Gtk.RadioButton(None, label=_('Main Panel Button'))
        self.activedict[self.commandname + "_group"] = self.group
        hbox.pack_start(self.group, False, False, 0)
        self.group.show()
        ivbox.pack_start(hbox, False, False, 0)
        hbox.show()

        self.groups_adj = Gtk.Adjustment(
            value=1.0,
            lower=1.0,
            upper=PGlobs.num_micpairs * 2,
            step_increment=1.0)
        self.valuesdict[self.commandname + "_groupnum"] = self.groups_adj
        groups_spin = Gtk.SpinButton(
            adjustment=self.groups_adj, climb_rate=0.0, digits=0)
        hbox.pack_end(groups_spin, False, False, 0)
        groups_spin.show()

        hbox = Gtk.HBox()
        hbox.set_spacing(6)
        ivbox.pack_start(hbox, False, False, 0)
        hbox.show()
        self.no_front_panel_opener = Gtk.RadioButton(None, label=_("This:"))
        self.activedict[self.commandname + "_using_local_opener"] = \
            self.no_front_panel_opener
        self.no_front_panel_opener.connect(
            "toggled",
            lambda w: self.open.set_sensitive(w.get_active()))
        hbox.pack_start(self.no_front_panel_opener, False, False, 0)
        self.no_front_panel_opener.show()
        self.no_front_panel_opener.join_group(self.group)

        self.openaction = Gtk.ToggleAction(None, _('Closed'), None, None)
        self.openaction.connect(
            "toggled",
            lambda w: w.set_label(
                _('Open')
                if w.get_active() else _('Closed')
            )
        )

        self.open.connect("toggled", self.cb_open)
        self.open.connect("toggled", self.sendnewstats, "open")
        hbox.pack_start(self.open, True, True, 0)
        self.open.show()
        self.open.set_related_action(self.openaction)
        self.open.emit("toggled")
        self.open.set_sensitive(False)
        self.fixups.append(lambda: self.open.emit("toggled"))

        self.pan = PanWidget(_('Stereo Panning'), commandname)
        self.pan.pan_active.connect("toggled", self.sendnewstats, "pan_active")
        self.fixups.append(lambda: self.pan.pan_active.emit("toggled"))
        self.pan.pan.connect("value-changed", self.sendnewstats, "pan")
        self.pan.pan.emit("value-changed")
        self.fixups.append(lambda: self.pan.pan.emit("value-changed"))
        self.valuesdict.update(self.pan.valuesdict)
        self.activedict.update(self.pan.activedict)

        self.vbox.pack_start(self.pan, False, False, 0)
        self.pan.show_all()

        # TC: A set of controls that perform audio signal matching.
        pairedframe = Gtk.Frame(label=" %s " % _('Signal Matching'))
        set_tip(
            pairedframe,
            _('These controls are provided to obtain a decent '
              'match between the two microphones.'))
        pairedframe.modes = (3, )
        self.vbox.pack_start(pairedframe, False, False, 0)
        pairedvbox = Gtk.VBox()
        pairedvbox.set_border_width(3)
        pairedframe.add(pairedvbox)
        pairedvbox.show()
        pairedmicgainadj = Gtk.Adjustment(
            value=0.0,
            lower=-20.0,
            upper=+20.0,
            step_increment=0.1,
            page_increment=2)
        pairedmicgain = self.numline(
            _('Relative Gain (dB)'),
            "pairedgain",
            digits=1,
            adj=pairedmicgainadj)
        pairedvbox.pack_start(pairedmicgain, False, False, 0)
        pairedmicgain.show()
        # TC: Mic audio phase inversion control.
        pairedinvert = self.check(_('Invert Signal'), "pairedinvert")
        pairedvbox.pack_start(pairedinvert, False, False, 0)
        pairedinvert.show()

        micgainadj = Gtk.Adjustment(
            value=0.0,
            lower=-20.0,
            upper=+30.0,
            step_increment=0.1,
            page_increment=2)
        invertaction = Gtk.ToggleAction(
            "invert",
            _('Invert Signal'),
            _('Useful for when microphones are cancelling one another '
              'out, producing a hollow sound.'), None)
        # TC: Control whether to mix microphone audio to the DJ mix.
        indjmixaction = Gtk.ToggleAction(
            "indjmix",
            _("In The DJ's Mix"),
            _('Make the microphone audio audible in the DJ mix. '
              'This may not always be desirable.'), None)

        self.simple_box = Gtk.VBox()
        self.simple_box.set_spacing(2)
        self.vbox.pack_start(self.simple_box, False, False, 0)
        self.simple_box.modes = (1, )

        ivbox = self.frame(" " + _('Basic Controls') + " ", self.simple_box)
        micgain = self.numline(
            _('Boost/Cut (dB)'),
            "gain",
            digits=1,
            adj=micgainadj)
        ivbox.pack_start(micgain, False, False, 0)

        invert_simple = self.check("", "invert")
        invert_simple.set_related_action(invertaction)
        ivbox.pack_start(invert_simple, False, False, 0)
        set_tip(
            invert_simple,
            _('Useful for when microphones are cancelling '
              'one another out, producing a hollow sound.')
        )

        indjmix = self.check("", "indjmix")
        indjmix.set_related_action(indjmixaction)
        ivbox.pack_start(indjmix, False, False, 0)
        set_tip(
            indjmix,
            _('Make the microphone audio audible in the DJ mix. '
              'This may not always be desirable.')
        )

        self.processed_box = Gtk.VBox()
        self.processed_box.modes = (2, )
        self.processed_box.set_spacing(2)
        self.vbox.pack_start(self.processed_box, False, False, 0)

        ivbox = self.frame(" %s " % _('High Pass Filter'), self.processed_box)
        hpcutoff = self.numline(_('Cutoff Frequency'), "hpcutoff",
                                100.0, 30.0, 120.0, 1.0, 1)
        ivbox.pack_start(hpcutoff, False, False, 0)
        # TC: User can set the number of filter stages.
        hpstages = self.numline(_('Stages'), "hpstages", 4.0, 1.0, 4.0, 1.0, 0)
        ivbox.pack_start(hpstages, False, False, 0)
        set_tip(
            ivbox,
            _('Frequency in Hertz above which audio can pass to later stages. '
              'Use this feature to restrict low frequency sounds such as '
              'mains hum. Setting too high a level will make your voice '
              'sound thin.')
        )

        # TC: this is the treble control. HF = high frequency.
        ivbox = self.frame(" " + _('HF Detail') + " ", self.processed_box)
        hfmulti = self.numline(_('Effect'), "hfmulti", 0.0, 0.0, 9.0, 0.1, 1)
        ivbox.pack_start(hfmulti, False, False, 0)
        hfcutoff = self.numline(_('Cutoff Frequency'), "hfcutoff",
                                2000.0, 900.0, 4000.0, 10.0, 0)
        ivbox.pack_start(hfcutoff, False, False, 0)
        set_tip(
            ivbox,
            _('You can use this to boost the amount of treble in the audio.')
        )

        # TC: this is the bass control. LF = low frequency.
        ivbox = self.frame(" " + _('LF Detail') + " ", self.processed_box)
        lfmulti = self.numline(_('Effect'), "lfmulti", 0.0, 0.0, 9.0, 0.1, 1)
        ivbox.pack_start(lfmulti, False, False, 0)
        lfcutoff = self.numline(_('Cutoff Frequency'), "lfcutoff",
                                150.0, 50.0, 400.0, 1.0, 0)
        ivbox.pack_start(lfcutoff, False, False, 0)
        set_tip(
            ivbox,
            _('You can use this to boost the amount of bass in the audio.')
        )

        # TC: lookahead brick wall limiter.
        ivbox = self.frame(" " + _('Limiter') + " ", self.processed_box)
        micgain = self.numline(
            _('Boost/Cut (dB)'),
            "gain",
            digits=1,
            adj=micgainadj
        )
        ivbox.pack_start(micgain, False, False, 0)
        # TC: this is the peak signal limit.
        limit = self.numline(
            _('Upper Limit'),
            "limit",
            -3.0, -9.0, 0.0, 0.5, 1)
        ivbox.pack_start(limit, False, False, 0)
        set_tip(
            ivbox,
            _('A look-ahead brick-wall limiter. Audio signals are '
              'capped at the upper limit.')
        )

        ivbox = self.frame(" " + _('Noise Gate') + " ", self.processed_box)
        # TC: noise gate triggers at this level.
        ng_thresh = self.numline(_('Threshold'), "ngthresh",
                                 -30.0, -62.0, -20.0, 1.0, 0)
        ivbox.pack_start(ng_thresh, False, False, 0)
        # TC: negative gain when the noise gate is active.
        ng_gain = self.numline(_('Gain'), "nggain", -6.0, -12.0, 0.0, 1.0, 0)
        ivbox.pack_start(ng_gain, False, False, 0)
        set_tip(
            ivbox,
            _("Reduce the unwanted quietest sounds and background "
              "noise which you don't want your listeners to hear with this.")
        )

        ivbox = self.frame(" " + _('De-esser') + " ", self.processed_box)
        # TC: Bias has a numeric setting.
        ds_bias = self.numline(
            _('Bias'), "deessbias", 0.35, 0.1, 10.0, 0.05, 2)
        ivbox.pack_start(ds_bias, False, False, 0)
        # TC: The de-esser attenuation in ess-detected state.
        ds_gain = self.numline(
            _('Gain'), "deessgain", -4.5, -10.0, 0.0, 0.5, 1)
        ivbox.pack_start(ds_gain, False, False, 0)
        set_tip(
            ivbox,
            _('Reduce the S, T, and P sounds which microphones tend '
              'to exaggerate. Ideally the Bias control will be set '
              'low so that the de-esser is off when there is silence '
              'but is set high enough that mouse clicks are detected '
              'and suppressed.')
        )

        ivbox = self.toggle_frame(
            _('Ducker'), "duckenable", self.processed_box)
        duckrelease = self.numline(_('Release'), "duckrelease",
                                   400.0, 100.0, 999.0, 10.0, 0)
        ivbox.pack_start(duckrelease, False, False, 0)
        duckhold = self.numline(_('Hold'), "duckhold",
                                350.0, 0.0, 999.0, 10.0, 0)
        ivbox.pack_start(duckhold, False, False, 0)
        set_tip(
            ivbox,
            _('The ducker automatically reduces the level of player '
              'audio when the DJ speaks. These settings allow you to adjust'
              ' the timings of that audio reduction.'))

        ivbox = self.frame(" " + _('Other options') + " ", self.processed_box)

        invert_complex = self.check("", NotImplemented, save=False)
        invert_complex.set_related_action(invertaction)
        ivbox.pack_start(invert_complex, False, False, 0)
        set_tip(
            invert_complex,
            _('Useful for when microphones are cancelling '
              'one another out, producing a hollow sound.'))
        phaserotate = self.check(_('Phase Rotator'), "phaserotate")
        ivbox.pack_start(phaserotate, False, False, 0)
        set_tip(
            phaserotate,
            _('This feature processes the microphone audio '
              'so that it sounds more even. The effect is '
              'particularly noticable on male voices.'))
        indjmix = self.check("", NotImplemented, save=False)
        indjmix.set_related_action(indjmixaction)
        ivbox.pack_start(indjmix, False, False, 0)
        set_tip(
            indjmix,
            _('Make the microphone audio audible in the DJ mix. '
              'This may not always be desirable.')
        )

        self.mode.set_active(0)
        indjmix.set_active(True)


class mixprefs:

    def send_new_resampler_stats(self):
        self.parent.mixer_write("RSQT=%d\nACTN=resamplequality\nend\n"
                                % self.resample_quality)

    def cb_resample_quality(self, widget, data):
        if widget.get_active():
            self.resample_quality = data
            self.send_new_resampler_stats()

    def cb_dither(self, widget, data=None):
        if widget.get_active():
            string_to_send = "ACTN=dither\nend\n"
        else:
            string_to_send = "ACTN=dontdither\nend\n"
        self.parent.mixer_write(string_to_send)

    def cb_vol_changed(self, widget):
        self.parent.send_new_mixer_stats()

    def cb_restore_session(self, widget, data=None):
        state = not widget.get_active()
        for each in (self.lpconfig, self.rpconfig, self.misc_session_frame):
            each.set_sensitive(state)

    def delete_event(self, widget, event, data=None):
        self.window.hide()
        return True

    def save_resource_template(self):
        try:
            with open(pm.basedir / "config", "w") as f:
                f.write("[resource_count]\n")
                for name, widget in self.rrvaluesdict.items():
                    print(name, widget)
                    f.write(name + "=" + str(int(widget.get_value())) + "\n")
                f.write("num_effects=%d\n" %
                        (24 if self.more_effects.get_active() else 12))
        except IOError:
            print("Error while writing out player defaults")

    def save_player_prefs(self, where=None):
        try:
            with open((where or pm.basedir) / "playerdefaults", "w") as f:
                for name, widget in self.activedict.items():
                    f.write(name + "=" + str(int(widget.get_active())) + "\n")
                for name, widget in self.valuesdict.items():
                    f.write(name + "=" + str(widget.get_value()) + "\n")
                for name, widget in self.textdict.items():
                    text = widget.get_text()
                    if text is not None:
                        f.write(name + "=" + text + "\n")
                    else:
                        f.write(name + "=\n")
        except IOError:
            print("Error while writing out player defaults")

    def load_player_prefs(self):
        songdb_active = False
        try:
            file = open(pm.basedir / "playerdefaults")

            while 1:
                line = file.readline()
                if line == "":
                    break
                if line.count("=") != 1:
                    continue
                line = line.split("=")
                key = line[0].strip()
                value = line[1][:-1].strip()
                if key in self.activedict:
                    if value == "True":
                        value = True
                    elif value == "False":
                        value = False
                    else:
                        value = int(value)
                    if key == "songdb_active":
                        songdb_active = value
                    else:
                        self.activedict[key].set_active(value)
                elif key in self.valuesdict:
                    self.valuesdict[key].set_value(float(value))
                elif key in self.textdict:
                    self.textdict[key].set_text(value)
            file.close()
        except IOError:
            print("Failed to read playerdefaults file")
        if songdb_active:
            self.activedict["songdb_active"].set_active(songdb_active)
        self.parent.send_new_mixer_stats()

    def apply_player_prefs(self):
        for each in (self.lpconfig, self.rpconfig):
            each.apply()

        if self.startmini.get_active():
            self.mini.clicked()

        if self.tracks_played.get_active():
            self.parent.history_expander.set_expanded(True)
            self.parent.history_vbox.show()
        if self.stream_mon.get_active():
            self.parent.listen_stream.set_active(True)

    def callback(self, widget, data):
        parent = self.parent
        if data == "basic streamer":
            if parent.feature_set.get_active():
                parent.feature_set.set_active(False)
        if data == "fully featured":
            if not parent.feature_set.get_active():
                parent.feature_set.set_active(True)
        if data == "enhanced-crossfader":
            if widget.get_active():
                parent.listen.show()
                parent.passleft.show()
                parent.passright.show()
                parent.passspeed.show()
                parent.passbutton.show()
            else:
                parent.listen.hide()
                parent.passleft.hide()
                parent.passright.hide()
                parent.passspeed.hide()
                parent.passbutton.hide()
                parent.listen.set_active(False)
        if data == "bigger box":
            if widget.get_active():
                self.parent.player_left.digiprogress.set_width_chars(7)
                self.parent.player_right.digiprogress.set_width_chars(7)
            else:
                self.parent.player_left.digiprogress.set_width_chars(6)
                self.parent.player_right.digiprogress.set_width_chars(6)
        if data == "tooltips":
            if widget.get_active():
                MAIN_TIPS.enable()
            else:
                MAIN_TIPS.disable()

    def cb_mic_boost(self, widget):
        self.parent.send_new_mixer_stats()

    def cb_pbspeed(self, widget):
        if widget.get_active():
            self.parent.player_left.pbspeedbar.set_value(64.0)
            self.parent.player_right.pbspeedbar.set_value(64.0)
            self.parent.player_left.pbspeedbox.show()
            self.parent.player_right.pbspeedbox.show()
            self.parent.jingles.interlude.pbspeedbar.set_value(64.0)
            self.parent.jingles.interlude.pbspeedbox.show()
        else:
            self.parent.player_left.pbspeedbox.hide()
            self.parent.player_right.pbspeedbox.hide()
            self.parent.jingles.interlude.pbspeedbox.hide()
        self.parent.send_new_mixer_stats()

    def cb_dual_volume(self, widget):
        if widget.get_active():
            self.parent.deck2adj.set_value(self.parent.deckadj.get_value())
            self.parent.deck2vol.show()
            set_tip(self.parent.deckvol,
                    _('The volume control for the left music player.'))
        else:
            if self.parent.player_left.is_playing ^ \
                    self.parent.player_right.is_playing:
                if self.parent.player_left.is_playing:
                    self.parent.deck2adj.set_value(
                        self.parent.deckadj.get_value())
                else:
                    self.parent.deckadj.set_value(
                        self.parent.deck2adj.get_value())
            else:
                halfdelta = (self.parent.deck2adj.get_value() -
                             self.parent.deckadj.get_value()) / 2
                self.parent.deck2adj.props.value -= halfdelta
                self.parent.deckadj.props.value += halfdelta

            self.parent.deck2vol.hide()
            set_tip(self.parent.deckvol,
                    _('The volume control shared by both music players.'))

    def cb_rg_indicate(self, widget):
        show = widget.get_active()
        for each in (self.parent.player_left, self.parent.player_right,
                     self.parent.jingles.interlude):
            each.show_replaygain_markers(show)

    def cb_realize(self, window):
        self.wst.apply()

    def show_about(self):
        self.notebook.set_current_page(self.notebook.page_num(self.aboutframe))
        self.window.present()

    def mic_controls_backend_update(self):
        """Send mic preferences to the backend.

        This needs to be called whenever the backend is restarted.
        """
        for mic in self.mic_controls:
            for fixup in mic.fixups:
                fixup()

    def voip_pan_backend_update(self, widget=None):
        widget = self.voip_pan
        stringtosend = "VPAN=%d\nACTN=voippan\nend\n" % (
            widget.pan.get_value()
            if widget.pan_active.get_active() else -1)
        self.parent.mixer_write(stringtosend)

    def __init__(self, parent):
        self.parent = parent
        self.parent.prefs_window = self
        self.window = Gtk.Window(Gtk.WindowType.TOPLEVEL)
        self.window.set_size_request(-1, 480)
        self.window.connect("realize", self.cb_realize)
        self.parent.window_group.add_window(self.window)
        # TC: preferences window title.
        self.window.set_title(_('IDJC Preferences') + pm.title_extra)
        self.window.set_border_width(10)
        self.window.set_resizable(True)
        self.window.connect("delete_event", self.delete_event)
        self.window.set_destroy_with_parent(True)
        self.notebook = Gtk.Notebook()
        self.window.add(self.notebook)
        self.wst = WindowSizeTracker(self.window)

        # General tab

        generalwindow = Gtk.ScrolledWindow()
        generalwindow.set_border_width(8)
        generalwindow.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outervbox = Gtk.VBox()
        outervbox.set_spacing(5)
        generalwindow.add_with_viewport(outervbox)
        generalwindow.show()
        outervbox.set_border_width(3)

        aud_rs_hbox = Gtk.HBox()

        # User can use this to set the audio level in the headphones

        # TC: The DJ's sound level controller.
        frame = Gtk.Frame(label=" %s " % _('DJ Audio Level'))
        frame.set_label_align(0.5, 0.5)
        frame.set_border_width(3)
        hbox = Gtk.HBox()
        hbox.set_border_width(5)
        frame.add(hbox)
        hbox.show()
        self.dj_aud_adj = Gtk.Adjustment(
            value=0.0,
            lower=-60.0,
            upper=0.0,
            step_increment=0.5,
            page_increment=1.0)
        dj_aud = Gtk.SpinButton(
            adjustment=self.dj_aud_adj, climb_rate=1, digits=1)
        dj_aud.connect("value-changed", self.cb_vol_changed)
        hbox.pack_start(dj_aud, True, False, 0)
        dj_aud.show()
        set_tip(dj_aud, _('This adjusts the sound level of the DJ audio.'))
        aud_rs_hbox.pack_start(frame, False, False, 0)
        frame.show()

        # TC: The alarm sound level.
        frame = Gtk.Frame(label=" %s " % _('Alarm Level'))
        frame.set_label_align(0.5, 0.5)
        frame.set_border_width(3)
        hbox = Gtk.HBox()
        hbox.set_border_width(5)
        frame.add(hbox)
        hbox.show()
        self.alarm_aud_adj = Gtk.Adjustment(
            value=0.0,
            lower=-60.0,
            upper=0.0,
            step_increment=0.5,
            page_increment=1.0)
        alarm_aud = Gtk.SpinButton(
            adjustment=self.alarm_aud_adj, climb_rate=1, digits=1)
        alarm_aud.connect("value-changed", self.cb_vol_changed)
        hbox.pack_start(alarm_aud, True, False, 0)
        alarm_aud.show()
        set_tip(
            alarm_aud,
            _('This adjusts the sound level of the DJ alarm. '
              'Typically this should be set close to the dj '
              'audio level when using the \'%s\''
              ' feature, otherwise a bit louder.' %
              _('Music Loudness Compensation'))
        )
        aud_rs_hbox.pack_start(frame, False, False, 0)
        frame.show()

        # User can use this to set the resampled sound quality

        frame = Gtk.Frame(label=" %s " % _('Player Resample Quality'))
        frame.set_label_align(0.5, 0.5)
        frame.set_border_width(3)
        hbox = Gtk.HBox()
        hbox.set_border_width(5)
        set_tip(
            hbox,
            _('This adjusts the quality of the audio resampling method '
              'used whenever the sample rate of the music file currently '
              'playing does not match the sample rate of the JACK sound '
              'server. Best mode offers the best sound quality but also '
              'uses the most CPU (not recommended for systems built before '
              '2006). All these modes provide adequate sound quality.'))
        frame.add(hbox)
        hbox.show()
        self.best_quality_resample = Gtk.RadioButton(None, label=_('Best'))
        self.best_quality_resample.connect(
            "toggled", self.cb_resample_quality, 0)
        rsbox = Gtk.HBox()
        rsbox.pack_start(self.best_quality_resample, True, False, 0)
        rsbox.show()
        hbox.add(rsbox)
        self.best_quality_resample.show()
        self.good_quality_resample = Gtk.RadioButton(
            self.best_quality_resample, label=_('Medium'))
        self.good_quality_resample.connect(
            "toggled", self.cb_resample_quality, 1)
        rsbox = Gtk.HBox()
        rsbox.pack_start(self.good_quality_resample, True, False, 0)
        rsbox.show()
        hbox.add(rsbox)
        self.good_quality_resample.show()
        self.good_quality_resample.join_group(self.best_quality_resample)
        self.fast_resample = Gtk.RadioButton(
            self.good_quality_resample, label=_('Fast'))
        self.fast_resample.connect("toggled", self.cb_resample_quality, 2)
        rsbox = Gtk.HBox()
        rsbox.pack_start(self.fast_resample, True, False, 0)
        rsbox.show()
        hbox.add(rsbox)
        self.fast_resample.show()
        self.fast_resample.join_group(self.best_quality_resample)

        aud_rs_hbox.pack_start(frame, True, True, 0)
        frame.show()

        outervbox.pack_start(aud_rs_hbox, False, False, 0)
        aud_rs_hbox.show()

        # TC: the set of features - section heading.
        featuresframe = Gtk.Frame(label=" %s " % _('Feature Set'))
        featuresframe.set_border_width(3)
        featuresvbox = Gtk.VBox()
        hbox = Gtk.HBox()
        hbox.set_border_width(2)
        featuresvbox.pack_start(hbox, False, False, 0)
        featuresframe.add(featuresvbox)
        featuresvbox.show()
        outervbox.pack_start(featuresframe, False, False, 0)
        featuresframe.show()
        vbox = Gtk.VBox()
        # TC: Start in the full featured user interface mode.
        self.startfull = Gtk.RadioButton(None, label=_('Start Full'))
        self.startfull.set_border_width(2)
        vbox.pack_start(self.startfull, False, False, 0)
        self.startfull.show()
        set_tip(self.startfull,
                _('Indicates which mode IDJC will be in when launched.'))

        # TC: Start in a reduced user interface mode.
        self.startmini = Gtk.RadioButton(None, label=_('Start Mini'))
        self.startmini.set_border_width(2)
        vbox.pack_start(self.startmini, False, False, 0)
        self.startmini.show()
        self.startmini.join_group(self.startfull)
        set_tip(self.startmini,
                _('Indicates which mode IDJC will be in when launched.'))

        vbox.show()
        hbox2 = Gtk.HBox()
        hbox2.set_border_width(10)
        hbox2.set_spacing(20)
        hbox.pack_start(hbox2, True, False, 0)

        self.maxi = Gtk.Button(" %s " % _('Fully Featured'))
        self.maxi.connect("clicked", self.callback, "fully featured")
        hbox2.pack_start(self.maxi, False, False, 0)
        self.maxi.show()
        set_tip(self.maxi,
                _('Run in full functionality mode which uses more CPU power.'))

        self.mini = Gtk.Button(" %s " % _('Basic Streamer'))
        self.mini.connect("clicked", self.callback, "basic streamer")
        hbox2.pack_start(self.mini, False, False, 0)
        self.mini.show()
        set_tip(
            self.mini,
            _('Run in a reduced functionality mode that lowers '
              'the burden on the CPU and takes up less screen space.'))

        hbox2.show()
        hbox.pack_start(vbox, False, False, 9)
        hbox.show()

        requires_restart = Gtk.Frame(
            label=" %s " %
            _('These settings take effect after restarting')
        )
        requires_restart.set_border_width(7)
        featuresvbox.pack_start(requires_restart, False, False, 0)
        requires_restart.show()

        rrvbox = Gtk.VBox()
        rrvbox.set_border_width(9)
        rrvbox.set_spacing(4)
        requires_restart.add(rrvbox)
        rrvbox.show()

        def hjoin(*widgets):
            hbox = Gtk.HBox()
            hbox.set_spacing(3)
            for w in widgets:
                hbox.pack_start(w, False, False, 0)
                w.show()
            hbox.show()
            return hbox

        self.more_effects = Gtk.RadioButton(
            None,
            label=_('Reserve 24 sound effects slots')
        )
        fewer_effects = Gtk.RadioButton(self.more_effects, label=_("Only 12"))
        fewer_effects.join_group(self.more_effects)
        if PGlobs.num_effects == 24:
            self.more_effects.clicked()
        else:
            fewer_effects.clicked()

        rrvbox.pack_start(
            hjoin(self.more_effects, fewer_effects),
            False,
            False,
            0
        )

        self.mic_qty_adj = Gtk.Adjustment(
            value=PGlobs.num_micpairs * 2,
            lower=2.0,
            upper=12.0,
            step_increment=2.0)
        spin = Gtk.SpinButton(adjustment=self.mic_qty_adj)
        rrvbox.pack_start(
            hjoin(
                spin,
                Gtk.Label(
                    label=_('Audio input channels')
                )
            ),
            False,
            False,
            0)

        self.stream_qty_adj = Gtk.Adjustment(
            value=PGlobs.num_streamers,
            lower=1.0,
            upper=9.0,
            step_increment=1.0)
        spin = Gtk.SpinButton(adjustment=self.stream_qty_adj)
        rrvbox.pack_start(
            hjoin(
                spin,
                Gtk.Label(
                    label=_('Simultaneous stream(s)')
                )
            ),
            False,
            False,
            0)

        self.recorder_qty_adj = Gtk.Adjustment(
            value=PGlobs.num_recorders,
            lower=0.0,
            upper=4.0,
            step_increment=1.0)
        spin = Gtk.SpinButton(adjustment=self.recorder_qty_adj)
        rrvbox.pack_start(
            hjoin(
                spin,
                Gtk.Label(
                    label=_('Simultaneous recording(s)')
                )
            ),
            False,
            False,
            0)

        self.rrvaluesdict = {
            "num_micpairs": self.mic_qty_adj,
            "num_streamers": self.stream_qty_adj,
            "num_recorders": self.recorder_qty_adj}

        # Meters on/off

        def showhide(toggle, target):
            if toggle.get_active():
                target.show()
            else:
                target.hide()
        frame = Gtk.Frame(label=" %s " % _('View'))
        frame.set_border_width(3)
        hbox = Gtk.HBox(3, True)
        hbox.set_border_width(10)
        frame.add(hbox)
        hbox.show()

        vbox = Gtk.VBox()
        hbox.pack_start(vbox, True, True, 0)
        vbox.show()
        self.show_stream_meters = Gtk.CheckButton()
        self.show_stream_meters.set_active(True)
        self.show_stream_meters.connect(
            "toggled", showhide, parent.streammeterbox)
        vbox.pack_start(self.show_stream_meters, False, False, 0)
        self.show_stream_meters.show()

        self.show_background_tracks_player = Gtk.CheckButton()
        self.show_background_tracks_player.set_active(True)
        self.show_background_tracks_player.connect(
            "toggled", showhide, parent.jingles.interlude_frame)
        vbox.pack_start(self.show_background_tracks_player, False, False, 0)
        self.show_background_tracks_player.show()

        self.show_button_bar = Gtk.CheckButton()
        self.show_button_bar.set_active(True)
        self.show_button_bar.connect("toggled", showhide, parent.hbox10)
        self.show_button_bar.connect("toggled", showhide, parent.hbox10spc)
        vbox.pack_start(self.show_button_bar, False, False, 0)
        self.show_button_bar.show()

        vbox = Gtk.VBox()
        hbox.pack_start(vbox, True, True, 0)
        vbox.show()
        self.show_microphones = Gtk.CheckButton()
        self.show_microphones.set_active(True)
        self.show_microphones.connect("toggled", showhide, parent.micmeterbox)
        vbox.pack_start(self.show_microphones, False, False, 0)
        self.show_microphones.show()

        self.no_mic_void_space = Gtk.CheckButton(
            _('Fill channel meter void space'))
        self.no_mic_void_space.set_active(True)
        for meter in parent.mic_meters:
            self.no_mic_void_space.connect("toggled", meter.always_show)
        vbox.pack_start(self.no_mic_void_space, False, False, 0)
        self.no_mic_void_space.show()

        outervbox.pack_start(frame, False, False, 0)
        frame.show()

        # ReplayGain controls

        frame = Gtk.Frame(label=" %s " % _('Player Loudness Normalisation'))
        frame.set_border_width(3)
        outervbox.pack_start(frame, False, False, 0)
        vbox = Gtk.VBox()
        frame.add(vbox)
        frame.show()
        vbox.set_border_width(10)
        vbox.set_spacing(1)
        vbox.show()

        self.rg_indicate = Gtk.CheckButton(
            _('Indicate which tracks have loudness metadata')
        )
        set_tip(
            self.rg_indicate,
            _('Shows a marker in the playlists next to'
              ' each track. Either a green circle or a red triangle.')
        )
        self.rg_indicate.connect("toggled", self.cb_rg_indicate)
        vbox.pack_start(self.rg_indicate, False, False, 0)
        self.rg_indicate.show()

        self.rg_adjust = Gtk.CheckButton(_('Adjust playback volume in dB'))
        set_tip(self.rg_adjust, _('Effective only on newly started tracks.'))
        vbox.pack_start(self.rg_adjust, False, False, 0)
        self.rg_adjust.show()

        table = Gtk.Table(2, 6)
        table.set_col_spacings(3)
        label = Gtk.Label(label=_('R128'))
        label.set_alignment(1.0, 0.5)
        r128_boostadj = Gtk.Adjustment(
            value=4.0, lower=-5.0, upper=25.5, step_increment=0.5)
        self.r128_boost = Gtk.SpinButton(
            adjustment=r128_boostadj, climb_rate=0.0, digits=1)
        set_tip(
            self.r128_boost,
            _('It may not be desirable to use the '
              'default level since it is rather quiet. This should be'
              ' set 4 or 5 dB higher than the ReplayGain setting.')
        )
        table.attach(label, 0, 1, 0, 1)
        table.attach(self.r128_boost, 1, 2, 0, 1)
        label = Gtk.Label(label=_('ReplayGain'))
        label.set_alignment(1.0, 0.5)
        rg_boostadj = Gtk.Adjustment(
            value=0.0,
            lower=-10.0,
            upper=20.5,
            step_increment=0.5
        )
        self.rg_boost = Gtk.SpinButton(
            adjustment=rg_boostadj,
            climb_rate=0.0,
            digits=1
        )
        set_tip(
            self.rg_boost,
            _('It may not be desirable to use the default'
              ' level since it is rather quiet. This should be set'
              ' 4 or 5 dB lower than the R128 setting.'))
        table.attach(label, 2, 3, 0, 1)
        table.attach(self.rg_boost, 3, 4, 0, 1)
        label = Gtk.Label(label=_('Untagged'))
        label.set_alignment(1.0, 0.5)
        rg_defaultgainadj = Gtk.Adjustment(
            value=-8.0, lower=-30.0, upper=10.0, step_increment=0.5)
        self.rg_defaultgain = Gtk.SpinButton(
            adjustment=rg_defaultgainadj, climb_rate=0.0, digits=1)
        set_tip(
            self.rg_defaultgain,
            _('Set this so that any unmarked tracks'
              ' are playing at a roughly similar loudness '
              'level as the marked ones.'))
        table.attach(label, 4, 5, 0, 1)
        table.attach(self.rg_defaultgain, 5, 6, 0, 1)

        label = Gtk.Label(label=_('All'))
        label.set_alignment(1.0, 0.5)
        all_boostadj = Gtk.Adjustment(
            value=0.0, lower=-10.0, upper=10.0, step_increment=0.5)
        self.all_boost = Gtk.SpinButton(
            adjustment=all_boostadj, climb_rate=0.0, digits=1)
        set_tip(
            self.all_boost,
            _('A master level control for the media players.')
        )
        table.attach(label, 0, 1, 1, 2)
        table.attach(self.all_boost, 1, 2, 1, 2)

        vbox.pack_start(table, False, False, 0)
        table.set_col_spacing(1, 7)
        table.set_col_spacing(3, 7)
        table.show_all()

        # Recorder filename format may be desirable to change for FAT32
        # compatibility

        frame = Gtk.Frame(
            label=" %s " %
            _('Recorder Filename (excluding the file extension)')
        )
        set_tip(
            frame,
            _("The specifiers are $r for the number of the "
              "recorder with the rest being documented in the "
              "strftime man page.\nUsers may wish to alter this "
              "to make filenames that are compatible with particular "
              "filesystems.")
        )
        frame.set_border_width(3)
        align = Gtk.Alignment.new(0, 0, 0, 0)
        align.props.xscale = 1.0
        self.recorder_filename = DefaultEntry("idjc.[%Y-%m-%d][%H:%M:%S].$r")
        align.add(self.recorder_filename)
        self.recorder_filename.show()
        align.set_border_width(3)
        frame.add(align)
        align.show()
        outervbox.pack_start(frame, True, False, 0)
        frame.show()

        # Miscellaneous Features

        frame = Gtk.Frame(label=" " + _('Miscellaneous Features') + " ")
        frame.set_border_width(3)
        vbox = Gtk.VBox()
        frame.add(vbox)
        frame.show()
        vbox.set_border_width(10)
        vbox.set_spacing(1)

        self.silence_killer = Gtk.CheckButton(
            _('Trim quiet song endings and trailing silence'))
        self.silence_killer.set_active(True)
        vbox.pack_start(self.silence_killer, False, False, 0)
        self.silence_killer.show()

        self.bonus_killer = Gtk.CheckButton(
            _('End tracks containing long passages of silence'))
        self.bonus_killer.set_active(True)
        vbox.pack_start(self.bonus_killer, False, False, 0)
        self.bonus_killer.show()

        self.speed_variance = Gtk.CheckButton(
            _('Enable the main-player speed/pitch controls'))
        vbox.pack_start(self.speed_variance, False, False, 0)
        self.speed_variance.connect("toggled", self.cb_pbspeed)
        self.speed_variance.show()
        set_tip(
            self.speed_variance,
            _('This option causes some extra widgets '
              'to appear below the playlists which '
              'allow the playback speed to be '
              'adjusted from 25% to 400% and a normal speed button.')
        )

        self.dual_volume = Gtk.CheckButton(
            _('Separate left/right player volume faders'))
        vbox.pack_start(self.dual_volume, False, False, 0)
        self.dual_volume.connect("toggled", self.cb_dual_volume)
        self.dual_volume.show()
        set_tip(
            self.dual_volume,
            _('Select this option to use an independent '
              'volume fader for the left and right music players.')
        )

        self.bigger_box_toggle = Gtk.CheckButton(
            _('Enlarge the time elapsed/remaining windows'))
        vbox.pack_start(self.bigger_box_toggle, False, False, 0)
        self.bigger_box_toggle.connect("toggled", self.callback, "bigger box")
        self.bigger_box_toggle.show()
        set_tip(
            self.bigger_box_toggle,
            _("The time elapsed/remaining windows "
              "sometimes don't appear big enough "
              "for the text that appears in them "
              "due to unusual DPI settings or the "
              "use of a different rendering "
              "engine. This option serves to fix that.")
        )

        self.djalarm = Gtk.CheckButton(
            _('Sound an alarm when the music is due to end'))
        vbox.pack_start(self.djalarm, False, False, 0)
        self.djalarm.show()
        set_tip(
            self.djalarm,
            _('An alarm tone alerting the DJ that dead-air is'
              ' just nine seconds away. This also works when '
              'monitoring stream audio but the alarm tone is '
              'not sent to the stream.\n\nJACK freewheel mode '
              'will also be automatically disengaged.')
        )

        freewheel_show = self.parent.freewheel_button.enabler
        vbox.pack_start(freewheel_show, False, False, 0)
        freewheel_show.show()

        self.dither = Gtk.CheckButton(
            _('Apply dither to 16 bit PCM playback'))
        vbox.pack_start(self.dither, False, False, 0)
        self.dither.connect("toggled", self.cb_dither)
        self.dither.show()
        set_tip(
            self.dither,
            _('This feature maybe improves the sound quality '
              'a little when listening on a 24 bit sound card.')
        )

        self.enable_tooltips = Gtk.CheckButton(_('Enable tooltips'))
        self.enable_tooltips.connect("toggled", self.callback, "tooltips")
        vbox.pack_start(self.enable_tooltips, False, False, 0)
        self.enable_tooltips.show()
        set_tip(
            self.enable_tooltips,
            _('This, what you are currently reading,'
              ' is a tooltip. This feature turns them on or off.')
        )

        vbox.show()

        outervbox.pack_start(frame, False, False, 0)

        # Song database preferences and connect button.
        self.songdbprefs = self.parent.topleftpane.prefs_controls
        self.songdbprefs.dbtoggle.set_related_action(
            self.parent.menu.songdbmenu_a)
        outervbox.pack_start(self.songdbprefs, False, False, 0)

        # Widget for user interface label renaming.
        label_subst = self.parent.label_subst
        outervbox.pack_start(label_subst, False, False, 0)
        label_subst.set_border_width(3)
        label_subst.show_all()

        # Session to be saved, or initial settings preferences.
        frame = Gtk.Frame(label=" %s " % _('Player Settings At Startup'))
        frame.set_label_align(0.5, 0.5)
        frame.set_border_width(3)
        vbox = Gtk.VBox()
        frame.add(vbox)
        vbox.show()

        restoresessionhbox = Gtk.HBox()
        restoresessionhbox.set_border_width(8)
        restoresessionhbox.show()
        self.restore_session_option = Gtk.CheckButton(
            _('Restore the previous session'))
        vbox.pack_start(restoresessionhbox, False, False, 0)
        restoresessionhbox.pack_start(
            self.restore_session_option, False, False, 0)
        self.restore_session_option.show()
        set_tip(
            self.restore_session_option,
            _('When starting IDJC most of the main '
              'window settings will be as they '
              'were left. As an alternative you may '
              'specify below how you want the '
              'various settings to be when IDJC starts.')
        )

        hbox = Gtk.HBox(True)
        vbox.add(hbox)
        hbox.set_border_width(6)
        hbox.set_spacing(3)

        self.lpconfig = InitialPlayerConfig(
            _("Player 1"), parent.player_left, "l")
        self.rpconfig = InitialPlayerConfig(
            _("Player 2"), parent.player_right, "r")
        for each in self.lpconfig, self.rpconfig:
            hbox.pack_start(each, True, True, 0)

        hbox.show()

        self.misc_session_frame = Gtk.Frame()
        self.misc_session_frame.set_border_width(4)
        misc_startup = Gtk.HBox(True, 20)
        self.misc_session_frame.add(misc_startup)
        misc_startup.show()
        hbox2 = Gtk.HBox()
        hbox2.pack_start(self.misc_session_frame, True, True, 7)
        hbox2.show()

        vbox.pack_start(hbox2, False, False, 2)
        self.misc_session_frame.show()
        misc_startupl = Gtk.VBox()
        misc_startup.pack_start(misc_startupl, True, True, 5)
        misc_startupl.show()
        misc_startupr = Gtk.VBox()
        misc_startup.pack_start(misc_startupr, True, True, 5)
        misc_startupr.show()

        self.tracks_played = Gtk.CheckButton(_('Tracks Played'))
        misc_startupl.add(self.tracks_played)
        self.tracks_played.show()
        # TC: DJ hears the stream mix.
        self.stream_mon = Gtk.CheckButton(_('Monitor Stream Mix'))
        misc_startupr.add(self.stream_mon)
        self.stream_mon.show()

        self.restore_session_option.connect("toggled", self.cb_restore_session)
        self.restore_session_option.set_active(True)

        outervbox.pack_start(frame, False, False, 0)
        frame.show()

        # TC: A heading label for miscellaneous settings.
        features_label = Gtk.Label(label=_('General'))
        self.notebook.append_page(generalwindow, features_label)
        features_label.show()
        outervbox.show()

        # Channels tab

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_border_width(0)
        scrolled_window.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC
        )
        panevbox = Gtk.VBox()
        scrolled_window.add_with_viewport(panevbox)
        scrolled_window.show()
        panevbox.set_border_width(3)
        panevbox.set_spacing(3)
        panevbox.get_parent().set_shadow_type(Gtk.ShadowType.NONE)
        panevbox.show()

        # Opener buttons for channels

        opener_settings = parent.mic_opener.opener_settings
        panevbox.pack_start(opener_settings, False, False, 3)

        # Individual channel settings

        self.mic_controls = mic_controls = []
        vbox = Gtk.VBox()
        for i in range(PGlobs.num_micpairs):
            uhbox = Gtk.HBox(True)
            vbox.pack_start(uhbox, False, False, 0)
            uhbox.show()
            lhbox = Gtk.HBox()
            vbox.pack_start(lhbox, False, False, 0)
            lhbox.show()
            for j in range(2):
                n = i * 2 + j
                micname = "mic_control_%d" % n
                c = AGCControl(self.parent, str(n + 1), micname, n)
                setattr(self, micname, c)
                uhbox.add(c)
                c.show()
                parent.mic_opener.add_mic(c)
                mic_controls.append(c)
            mic_controls[-2].set_partner(mic_controls[-1])
            mic_controls[-1].set_partner(mic_controls[-2])
        parent.mic_opener.finalise()

        panevbox.pack_start(vbox, False, False, 0)
        vbox.show()

        self.voip_pan = PanWidget(
            _('VoIP panning + mono downmix'),
            "voip_pan_widget"
        )
        self.voip_pan.pan_active.connect(
            "toggled",
            self.voip_pan_backend_update
        )
        self.voip_pan.pan.connect(
            "value-changed",
            self.voip_pan_backend_update
        )
        self.voip_pan.set_border_width(3)
        panevbox.pack_start(self.voip_pan, False, False, 0)
        self.voip_pan.show_all()

        label = Gtk.Label(label=_('Channels'))
        self.notebook.append_page(scrolled_window, label)
        label.show()

        # Controls tab
        tab = midicontrols.ControlsUI(self.parent.controls)
        # TC: Keyboard and MIDI bindings configuration.
        label = Gtk.Label(label=_('Bindings'))
        self.notebook.append_page(tab, label)
        tab.show()
        label.show()

        # about tab

        self.aboutframe = Gtk.Frame()
        frame.set_border_width(9)
        vbox = Gtk.VBox()
        self.aboutframe.add(vbox)
        label = Gtk.Label()
        label.set_markup('<span font_desc="sans italic 20">' +
                         self.parent.appname + '</span>')
        vbox.pack_start(label, False, False, 13)
        label.show()
        label = Gtk.Label()
        label.set_markup('<span font_desc="sans 13">Version ' +
                         self.parent.version + '</span>')
        vbox.pack_start(label, False, False, 0)
        label.show()

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(FGlobs.pkgdatadir / "logo.png")
        image = Gtk.Image()
        image.set_from_pixbuf(pixbuf)
        vbox.pack_start(image, False, False, 8)
        image.show()

        label = Gtk.Label()
        label.set_markup('<span font_desc="sans 13">' +
                         self.parent.copyright + '</span>')
        vbox.pack_start(label, False, False, 12)
        label.show()

        label = Gtk.Label()
        label.set_markup(
            '<span font_desc="sans 10">' + PGlobs.license + '</span>')
        vbox.pack_start(label, False, False, 1)
        label.show()

        nb = Gtk.Notebook()
        nb.set_border_width(10)
        vbox.pack_start(nb, True, True, 0)
        nb.show()

        lw = licence_window.LicenceWindow()
        lw.set_border_width(1)
        lw.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        label = Gtk.Label(label=_('Licence'))
        nb.append_page(lw, label)
        lw.show()
        label.show()

        def contribs_page(title, content):
            sw = Gtk.ScrolledWindow()
            sw.set_border_width(1)
            sw.set_shadow_type(Gtk.ShadowType.NONE)
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            label = Gtk.Label(label=title)
            nb.append_page(sw, label)
            sw.show()
            lw.show()
            ivbox = Gtk.VBox()
            sw.add_with_viewport(ivbox)
            ivbox.show()
            for each in content:
                label = Gtk.Label(label=each)
                label.set_use_markup(True)
                ivbox.add(label)
                label.show()

        contribs_page(
            _('Contributors'),
            ("Stephen Fairchild (s-fairchild@users.sourceforge.net)",
            "And Clover (and@doxdesk.com)",
            "Dario Abatianni (eisfuchs@users.sourceforge.net)",
            "Stefan Fendt (stefan@sfendt.de)",
            "Brian Millham (bmillham@users.sourceforge.net)"))

        contribs_page(_('Translators'),
            ("<b>es</b> Blank Frank (frank@rhizomatica.org)",
            "<b>fr</b> nvignot (nicotux@users.sf.net)",
            "<b>it</b>  Raffaele Morelli (raffaele.morelli@gmail.com)"))

        label = Gtk.Label(label=_('Build Info'))
        ivbox = Gtk.VBox()
        ivbox.set_spacing(10)
        ivbox.set_border_width(10)
        nb.append_page(ivbox, label)
        ivbox.show()

        with open(FGlobs.pkgdatadir / "buildinfo") as f:
            for each in f:
                label = Gtk.Label(label=each.rstrip())
                label.set_use_markup(True)
                label.set_selectable(True)
                ivbox.pack_start(label, False, False, 0)
                label.show()

        vbox.show()

        aboutlabel = Gtk.Label(label=_('About'))
        self.notebook.append_page(self.aboutframe, aboutlabel)
        aboutlabel.show()
        self.aboutframe.show()

        self.notebook.show()

        # These on by default
        self.djalarm.set_active(True)
        self.dither.set_active(True)
        self.fast_resample.set_active(True)
        self.enable_tooltips.set_active(True)

        # Default mic/aux configuration
        try:
            mic_controls[0].mode.set_active(2)
            mic_controls[0].alt_name.set_text("DJ")
            t = parent.mic_opener.ix2button[1].opener_tab
            t.button_text.set_text("DJ")
            t.icb.set_filename(FGlobs.pkgdatadir / "mic4.png")
            t.headroom.set_value(3)
            t.has_reminder_flash.set_active(True)
            t.is_microphone.set_active(True)
            t.freewheel_cancel.set_active(True)
            for cb, state in zip(iter(t.open_triggers.values()), (1, 1, 0, 1)):
                cb.set_active(state)
            if len(mic_controls) >= 4:
                mic_controls[2].mode.set_active(1)
                mic_controls[2].alt_name.set_text("Aux L")
                mic_controls[2].groups_adj.set_value(2)
                mic_controls[2].pan.pan_active.set_active(True)
                mic_controls[2].pan.set_values(0)
                mic_controls[3].mode.set_active(3)
                mic_controls[3].alt_name.set_text("Aux R")
                mic_controls[3].pan.pan_active.set_active(True)
                mic_controls[3].pan.set_values(100)
                t = parent.mic_opener.ix2button[2].opener_tab
                t.button_text.set_text("Aux")
                t.icb.set_filename(FGlobs.pkgdatadir / "jack2.png")
                list(t.open_triggers.values())[2].set_active(True)
        except (KeyError, IndexError):
            pass

        self.show_stream_meters.set_related_action(
            self.parent.menu.strmetersmenu_a)
        self.show_microphones.set_related_action(
            self.parent.menu.chmetersmenu_a)
        self.show_background_tracks_player.set_related_action(
            self.parent.menu.backgroundtracksmenu_a)
        self.show_button_bar.set_related_action(
            self.parent.menu.buttonbarmenu_a)

        self.show_stream_meters.set_active(True)
        self.show_microphones.set_active(True)
        self.show_background_tracks_player.set_active(True)
        self.show_button_bar.set_active(True)

        # Widgets to save that have the get_active method.
        self.activedict = {
            "startmini": self.startmini,
            "dsp_toggle": self.parent.dsp_button,
            "djalarm": self.djalarm,
            "trxpld": self.tracks_played,
            "strmon": self.stream_mon,
            "bigdigibox": self.bigger_box_toggle,
            "dither": self.dither,
            "recallsession": self.restore_session_option,
            "best_rs": self.best_quality_resample,
            "good_rs": self.good_quality_resample,
            "fast_rs": self.fast_resample,
            "speed_var": self.speed_variance,
            "dual_volume": self.dual_volume,
            "showtips": self.enable_tooltips,
            "silencekiller": self.silence_killer,
            "bonuskiller": self.bonus_killer,
            "rg_indicate": self.rg_indicate,
            "rg_adjust": self.rg_adjust,
            "str_meters": self.show_stream_meters,
            "mic_meters": self.show_microphones,
            "btn_bar": self.show_button_bar,
            "bg_tracks": self.show_background_tracks_player,
            "mic_meters_no_void": self.no_mic_void_space,
            "players_visible": self.parent.menu.playersmenu_i
        }

        for each in itertools.chain(
            mic_controls,
            (self.parent.freewheel_button, self.songdbprefs,
             self.lpconfig, self.rpconfig, opener_settings,
             label_subst, self.voip_pan)):
            self.activedict.update(each.activedict)

        self.valuesdict = {  # These widgets all have the get_value method.
            "effects1_vol": self.parent.jingles.jvol_adj[0],
            "effects1_muting": self.parent.jingles.jmute_adj[0],
            "effects2_vol": self.parent.jingles.jvol_adj[1],
            "effects2_muting": self.parent.jingles.jmute_adj[1],
            "voiplevel": self.parent.voipgainadj,
            "voipmixback": self.parent.mixbackadj,
            "interlude_vol": self.parent.jingles.ivol_adj,
            "passspeed": self.parent.passspeed_adj,
            "djvolume": self.dj_aud_adj,
            "alarmvolume": self.alarm_aud_adj,
            "rg_default": self.rg_defaultgain,
            "rg_boost": self.rg_boost,
            "r128_boost": self.r128_boost,
            "all_boost": self.all_boost
        }

        for each in itertools.chain(mic_controls, (
            opener_settings,
                self.voip_pan)):
            self.valuesdict.update(each.valuesdict)

        self.textdict = {  # These widgets all have the get_text method.
            "ltfilerqdir": self.parent.player_left.file_requester_start_dir,
            "rtfilerqdir": self.parent.player_right.file_requester_start_dir,
            "main_full_wst": self.parent.full_wst,
            "main_min_wst": self.parent.min_wst,
            "prefs_wst": self.wst,
            "rec_filename": self.recorder_filename
        }

        for each in itertools.chain(mic_controls, (
            opener_settings,
                label_subst, self.songdbprefs)):
            self.textdict.update(each.textdict)

        self.rangewidgets = (self.parent.deckadj,)
