#   midicontrols.py: MIDI and hotkey controls for IDJC
#   Copyright (C) 2010 Andrew Clover (and@doxdesk.com)
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

import sys
import re
import os.path
import time
import collections
import gettext
import functools

from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import Pango
from .generictreemodel import GenericTreeModel
import dbus
import dbus.service

from idjc import FGlobs, PGlobs
from .gtkstuff import threadslock
from .gtkstuff import timeout_add, source_remove
from .prelims import ProfileManager
from .tooltips import set_tip


_ = gettext.translation(FGlobs.package_name, FGlobs.localedir,
                        fallback=True).gettext

PM = ProfileManager()


control_methods = {
    # TC: Control method. Please keep it as Target:Action.
    'c_tips': _('Tooltips enable'),
    # TC: Control method. Please keep it as Target:Action.
    'c_sdjmix': _('DJ-mix monitor'),
    # TC: Control method. Please keep it as Target:Action.
    'l_panpre': _('Panning load from presets'),
    # TC: Control method. Please keep it as Target:Action.
    'p_pp': _('Player play/pause'),
    # TC: Control method. Please keep it as Target:Action.
    'p_stop': _('Player stop'),
    # TC: Control method. Please keep it as Target:Action.
    'p_advance': _('Player advance'),
    # TC: Control method. Please keep it as Target:Action.
    'p_prev': _('Player play previous'),
    # TC: Control method. Please keep it as Target:Action.
    'p_next': _('Player play next'),
    # TC: Control method. Please keep it as Target:Action.
    'p_sfire': _('Player play selected from start'),
    # TC: Control method. Please keep it as Target:Action.
    'p_sprev': _('Player select previous'),
    # TC: Control method. Please keep it as Target:Action.
    'p_snext': _('Player select next'),
    # TC: Control method. Please keep it as Target:Action.
    'p_stream': _('Player stream output enable'),
    # TC: Control method. Please keep it as Target:Action.
    'p_listen': _('Player DJ output enable'),
    # TC: Control method. Please keep it as Target:Action.
    'p_prep': _('Player DJ-only switch'),
    # TC: Control method. Please keep it as Target:Action.
    'p_vol': _('Player set volume'),
    # TC: Control method. Please keep it as Target:Action.
    'p_gain': _('Player set gain'),
    # TC: Control method. Please keep it as Target:Action.
    'p_pan': _('Player set balance'),
    # TC: Control method. Please keep it as Target:Action.
    'p_pitch': _('Player set pitchbend'),

    # TC: Control method. Please keep it as Target:Action.
    'p_tag': _('Playlist edit tags'),
    # TC: Control method. Please keep it as Target:Action.
    'p_istop': _('Playlist insert stop'),
    # TC: Control method. Please keep it as Target:Action.
    'p_istop2': _('Playlist insert stop 2'),
    # TC: Control method. Please keep it as Target:Action.
    'p_ianno': _('Playlist insert announce'),
    # TC: Control method. Please keep it as Target:Action.
    'p_itrans': _('Playlist insert transfer'),
    # TC: Control method. Please keep it as Target:Action.
    'p_ifade': _('Playlist insert crossfade'),
    # TC: Control method. Please keep it as Target:Action.
    'p_ipitch': _('Playlist insert pitchunbend'),
    # TC: Control method. Please keep it as Target:Action.
    'p_igotop': _('Playlist insert jump to top'),

    # TC: Control method. Please keep it as Target:Action.
    'x_fade': _('Players set crossfade'),
    # TC: Control method. Please keep it as Target:Action.
    'x_pass': _('Players pass crossfade'),
    # TC: Control method. Please keep it as Target:Action.
    'x_focus': _('Players set focus'),
    # TC: Control method. Please keep it as Target:Action.
    'x_pitch': _('Players show pitchbend'),
    # TC: Control method. Please keep it as Target:Action.
    'x_advance': _('Players advance'),

    # TC: Control method. Please keep it as Target:Action.
    'm_on': _('Channel output enable'),
    # TC: Control method. Please keep it as Target:Action.
    'm_vol': _('Channel set volume'),
    # TC: Control method. Please keep it as Target:Action.
    'm_gain': _('Channel set gain'),
    # TC: Control method. Please keep it as Target:Action.
    'm_pan': _('Channel set balance'),

    # TC: Control method. Please keep it as Target:Action.
    'v_on': _('VoIP output enable'),
    # TC: Control method. Please keep it as Target:Action.
    'v_prep': _('VoIP DJ-only switch'),
    # TC: Control method. Please keep it as Target:Action.
    'v_vol': _('VoIP set volume'),
    # TC: Control method. Please keep it as Target:Action.
    'v_mixback': _('VoIP set mixback'),
    # TC: Control method. Please keep it as Target:Action.
    'v_gain': _('VoIP set gain'),
    # TC: Control method. Please keep it as Target:Action.
    'v_pan': _('VoIP set balance'),

    # TC: Control method. Please keep it as Target:Action.
    'k_fire': _('Effect play from start'),

    # TC: Control method. Please keep it as Target:Action.
    'b_stop': _('Effects stop many'),
    # TC: Control method. Please keep it as Target:Action.
    'b_vol1': _('Effects set volume'),
    # TC: Control method. Please keep it as Target:Action.
    'b_vol2': _('Effects set headroom'),

    # TC: Control method. Please keep it as Target:Action.
    's_on': _('Stream set connected'),

    # TC: Control method. Please keep it as Target:Action.
    'r_on': _('Recorder set recording'),
}

control_targets = {
    'p': _('Player'),
    'm': _('Channel'),
    'k': _('Effect'),
    's': _('Stream'),
    'r': _('Recorder'),
    'l': _('Setting')
}

control_targets_players = (
    _('Left player'),
    _('Right player'),
    _('Background player'),
    _('Focused player'),
    _('Fadered player')
)

control_targets_effects_bank = (
    _('Effects bank 1'),
    _('Effects bank 2'),
    _('All effects')
)


class Binding(tuple):

    """Immutable value type representing an input bound to an action.

    An input is a MIDI event or keyboard movement. (Possibly others in future?)
    An action is a method of the Controls object, together with how to apply
    input to it, and, for some methods, a target integer specifying which
    player/channel/etc the method should be aimed at.

    A Binding is represented in string form in the 'controls' prefs file as
    one 'input:action' pair per line. There may be multiple bindings of the
    same input or the same action. An 'input' string looks like one of:

        Cc.nn   - MIDI control, channel 'c', control number 'nn'
        Nc.nn   - MIDI note, channel 'c', note id 'nn'
        Pc     - MIDI pitch wheel, channel 'c'
        Kmm.nnnn - Keypress, modifier-state 'm', keyval 'nnnn'

    All numbers are hex. This format is also used to send MIDI event data from
    the mixer to the idjcgui, with trailing ':vv' to set the value (0-127).

    An action string looks like:

        Mmethod.target.value

    Where method is the name of a method in the Controls object, target is
    the object index to apply it to where needed (eg. 0=left player for 'p_'
    methods), and the mode M is one of:

        D - mirror each input level change. For faders and held buttons.
            value may be 127, or -127 for inverted control (hold to set 0)
        P - call on input level high. For one-shot and toggle buttons.
            value is currently ignored.
        S - on input level high, set to specific value
            value is the value to set, from 0..127
        A - on input level high, alter value. For keyboard-controlled faders.
            value is the delta to add to current value, from -127..127

    Value is a signed decimal number. Example:

        C0.0F:Pp_stop.0.7F

    Binds the action 'Player 1 stop' to MIDI control number 15 on channel 0.
    """
    source = property(lambda self: self[0])
    channel = property(lambda self: self[1])
    control = property(lambda self: self[2])
    mode = property(lambda self: self[3])
    method = property(lambda self: self[4])
    target = property(lambda self: self[5])
    value = property(lambda self: self[6])

    # Possible source and mode values, in the order they should be listed in
    # the UI
    #
    SOURCES = (
        SOURCE_CONTROL,
        SOURCE_NOTE,
        SOURCE_PITCHWHEEL,
        SOURCE_KEYBOARD,
    ) = 'cnpk'

    MODES = (
        MODE_DIRECT,
        MODE_PULSE,
        MODE_SET,
        MODE_ALTER
    ) = 'dpsa'

    _default = [SOURCE_KEYBOARD, 0, 0x31, MODE_PULSE, 'p_pp', 0, 127]

    def __new__(cls, binding=None,
                source=None, channel=None, control=None,
                mode=None, method=None, target=None, value=None
                ):
        """New binding from copying old one, parsing from string, or new values
        """
        if binding is None:
            binding = list(cls._default)
        elif isinstance(binding, tuple):
            binding = list(binding)

        # Parse from string. Can also parse an input string alone
        #
        elif isinstance(binding, str):
            input_part, _, action_part = binding.partition(':')
            binding = list(cls._default)
            s = input_part[:1]
            if s not in Binding.SOURCES:
                raise ValueError('Unknown binding source %r' % input_part[0])
            binding[0] = s
            ch, _, inp = input_part[1:].partition('.')
            binding[1] = int(ch, 16)
            binding[2] = int(inp, 16)
            m = action_part[:1]
            if m not in Binding.MODES:
                raise ValueError('Unknown mode %r' % m)
            binding[3] = m
            parts = action_part[1:].split('.', 3)
            if len(parts) != 3:
                raise ValueError('Malformed control string %r' % action_part)
            if parts[0] not in Binding.METHODS:
                raise ValueError('Unknown method %r' % parts[0])
            binding[4] = parts[0]
            binding[5] = int(parts[1], 16)
            binding[6] = int(parts[2])
        else:
            raise ValueError('Expected string or Binding, not %r' % binding)

        # Override particular properties
        #
        if source is not None:
            binding[0] = source
        if channel is not None:
            binding[1] = channel
        if control is not None:
            binding[2] = control
        if mode is not None:
            binding[3] = mode
        if method is not None:
            binding[4] = method
        if target is not None:
            binding[5] = target
        if value is not None:
            binding[6] = value
        return tuple.__new__(cls, binding)

    def __str__(self):
        # Back to string
        #
        return '%s%x.%x:%s%s.%x.%d' % (self.source, self.channel, self.control,
                                       self.mode, self.method, self.target, self.value)

    def __repr__(self):
        return 'Binding(%r)' % str(self)

    @property
    def input_str(self):
        """Get user-facing representation of channel and control
        """
        if self.source == Binding.SOURCE_KEYBOARD:
            return '%s%s' % (self.channel_str, self.control_str.title())
        elif self.source == Binding.SOURCE_PITCHWHEEL:
            return self.channel_str
        else:
            return '%s: %s' % (self.channel_str, self.control_str)

    @property
    def channel_str(self):
        """Get user-facing representation of channel value (shifting for keys)
        """
        if self.source == Binding.SOURCE_KEYBOARD:
            return Binding.modifier_to_str(self.channel)
        else:
            return str(self.channel)
        return ''

    @property
    def control_str(self):
        """Get user-facing representation of control value (key, note, ...)
        """
        if self.source == Binding.SOURCE_KEYBOARD:
            return Binding.key_to_str(self.control)
        elif self.source == Binding.SOURCE_NOTE:
            return Binding.note_to_str(self.control)
        elif self.source == Binding.SOURCE_CONTROL:
            return str(self.control)
        return ''

    @property
    def action_str(self):
        """Get user-facing representation of action/mode/value
        """
        return control_methods[self.method]

    @property
    def modifier_str(self):
        """Get user-facing representation of interaction type and value
        """
        if self.mode == Binding.MODE_DIRECT:
            if self.value < 0:
                return ' (-)'
            elif getattr(Controls, self.method).action_modes[0] != \
                    Binding.MODE_DIRECT:
                return ' (+)'
        elif self.mode == Binding.MODE_SET:
            return ' (%d)' % self.value
        elif self.mode == Binding.MODE_ALTER:
            if self.value >= 0:
                return ' (+%d)' % self.value
            else:
                return ' (%d)' % self.value
        elif self.mode == Binding.MODE_PULSE:
            if self.value < 0x40:
                return ' (1-)'
        return ''

    @property
    def target_str(self):
        """Get user-facing representation of the target for this method
        """
        group = self.method[0]
        if group == 'p':
            return control_targets_players[self.target]
        if group == 'b':
            return control_targets_effects_bank[self.target]
        if group in control_targets:
            return '%s %d' % (control_targets[group], self.target+1)
        return ''

    # Display helpers used by the _str methods and also SpinButtons

    # Keys, with fallback names for unmapped keyvals
    #
    @staticmethod
    def key_to_str(k):
        name = Gdk.keyval_name(k)
        if name is None:
            return '<%04X>' % k
        return name

    @staticmethod
    def str_to_key(s):
        s = s.strip()
        if s.startswith('<') and s.endswith('>') and len(s) == 6:
            return int(s[1:-1], 16)

        # Try to find a name for a keyval using different case variants.
        # Unfortunately the case needed by keyval_from_name does not usually
        # match the case produced by keyval_name. Argh.
        #
        # Luckily it's not essential that this is completely right, as it's
        # only needed for bumping the 'key' spinbutton, which will rarely be
        # done.
        #
        if s.lower() == 'backspace':
            # TC: The name of the backspace key.
            s = _('BackSpace')
        n = Gdk.keyval_from_name(s)
        if n == 0:
            n = Gdk.keyval_from_name(s.lower())
        if n == 0:
            n = Gdk.keyval_from_name(s.title())
        if n == 0:
            n = Gdk.keyval_from_name(s[:1].upper()+s[1:].lower())
        return n

    # Note names. Convert to/from MIDI note/octave format.
    #
    NOTES = 'C,C#,D,D#,E,F,F#,G,G#,A,A#,B'.replace('#', '\u266F').split(',')

    @staticmethod
    def note_to_str(n):
        return '%s%d' % (Binding.NOTES[n % 12], n//12-1)

    @staticmethod
    def str_to_note(s):
        m = re.match('^([A-G](?:\u266F?))(-1|\d)$', s.replace(' ', '').replace(
            '#', '\u266F').upper())
        if m is None:
            raise ValueError('Invalid note')
        n = Binding.NOTES.index(m.group(1))
        n += int(m.group(2))*12+12
        if not 0 <= n < 128:
            raise ValueError('Octave out of range')
        return n

    # Shifting keys. Convert to/from short textual forms, with symbols rather
    # than the verbose names that accelgroup_name uses.
    #
    # Also convert to/from an ordinal form where the bits are reordered to fit
    # a simple 0..127 range, for easy use in a SpinButton.
    #
    MODIFIERS = (
        (Gdk.ModifierType.SHIFT_MASK, '\u21D1'),
        (Gdk.ModifierType.CONTROL_MASK, '^'),
        (Gdk.ModifierType.MOD1_MASK, '\u2020'),  # alt/option
        (Gdk.ModifierType.MOD5_MASK, '\u2021'),  # altgr/option
        #(Gdk.EventMask.META_MASK, '\u25C6'),
        # (Gdk.EventMask.SUPER_MASK, '\u2318'), # win/command
        #(Gdk.EventMask.HYPER_MASK, '\u25CF'),
    )
    MODIFIERS_MASK = (int(m) for m, c in MODIFIERS)

    @staticmethod
    def modifier_to_str(m):
        return ''.join(c for mask, c in Binding.MODIFIERS if m & mask != 0)

    @staticmethod
    def str_to_modifier(s):
        return sum(mask for mask, c in Binding.MODIFIERS if c in s)

    @staticmethod
    def modifier_to_ord(m):
        return sum(1 << i for i, (mask, c) in enumerate(
            Binding.MODIFIERS) if m & mask != 0)

    @staticmethod
    def ord_to_modifier(b):
        return sum(mask for i, (mask, c) in enumerate(
            Binding.MODIFIERS) if b & (1 << i) != 0)

    METHOD_GROUPS = []
    METHODS = []

# Decorator for control method type annotation. Method names will be stored in
# order in Binding.METHODS; the given modes will be added as a function
# property so the binding editor can read what modes to offer.
#


def action_method(*modes):
    def wrap(fn):
        fn.action_modes = modes
        Binding.METHODS.append(fn.__name__)
        group = fn.__name__[0]
        if group not in Binding.METHOD_GROUPS:
            Binding.METHOD_GROUPS.append(group)
        return fn
    return wrap


dbusify = functools.partial(
    dbus.service.method, dbus_interface=PGlobs.dbus_bus_basename)


# Controls ___________________________________________________________________


class RepeatCache(collections.MutableSet):

    """A smart keyboard repeat cache -- implements time to live.

    Downstrokes are logged along with the time. Additional downstrokes
    refresh the TTL value for the key. This is done through checking the
    cached Binding before the TTL has run out, otherwise the cached
    entry is removed.

    The __contains__ method runs the TTL cache purge.
    """

    @property
    def ttl(self):
        """Time To Live.

        The duration a keystroke is valid in the absence of repeats."""
        return self._ttl

    @ttl.setter
    def ttl(self, ttl):
        assert(isinstance(ttl, (float, int)))
        self._ttl = ttl

    def __init__(self, ttl=0.8):
        self.ttl = ttl
        self._cache = {}

    def __len__(self):
        return len(self._cache)

    def __iter__(self):
        return iter(self._cache)

    def __contains__(self, key):
        if key in self._cache:
            if self._cache[key] < time.time():
                del self._cache[key]
                return False
            else:
                self._cache[key] = time.time() + self._ttl
                return True
        else:
            return False

    def add(self, key):
        self._cache[key] = time.time() + self._ttl

    def discard(self, key):
        if key in self._cache:
            del self._cache[key]


class Controls(dbus.service.Object):

    """Dispatch and implementation of input events to action methods.
    """
    # List of controls set up, empty by default. Mapping of input ID to list
    # of associated control commands, each (control_id, n, mode, v)
    #
    settings = {}

    def __init__(self, owner):
        dbus.service.Object.__init__(
            self, PM.dbus_bus_name, PGlobs.dbus_objects_basename + "/controls")
        self.owner = owner
        self.learner = None
        self.editing = None
        self.lookup = {}
        self.highlights = {}
        self.repeat_cache = RepeatCache()

        # Default minimal set of bindings, if not overridden by prefs file
        # This matches the hotkeys previously built into IDJC
        #
        self.bindings = [
            Binding('k100.ffbe:pk_fire.0.127'),  # F-key effects
            Binding('k100.ffbf:pk_fire.1.127'),
            Binding('k100.ffc0:pk_fire.2.127'),
            Binding('k100.ffc1:pk_fire.3.127'),
            Binding('k100.ffc2:pk_fire.4.127'),
            Binding('k100.ffc3:pk_fire.5.127'),
            Binding('k100.ffc4:pk_fire.6.127'),
            Binding('k100.ffc5:pk_fire.7.127'),
            Binding('k100.ffc6:pk_fire.8.127'),
            Binding('k100.ffc7:pk_fire.9.127'),
            Binding('k100.ffc8:pk_fire.a.127'),
            Binding('k100.ffc9:pk_fire.b.127'),
            Binding('k100.ff1b:pb_stop.2.127'),  # Esc stop effects
            Binding('k100.31:sx_fade.b.0'),  # 1-2 xfader sides
            Binding('k100.32:sx_fade.b.127'),
            Binding('k100.63:px_pass.0.127'),  # C, pass xfader
            Binding('k100.6d:pm_on.0.127'),  # M, first channel toggle
            Binding('k100.76:pv_on.0.127'),  # V, VoIP toggle
            Binding('k100.70:pv_prep.0.127'),  # P, VoIP prefade
            # backspace, stop focused player
            Binding('k100.ff08:pp_stop.3.127'),
            # slash, advance xfaded player
            Binding('k100.2f:pp_advance.4.127'),
            Binding('k100.74:pp_tag.3.127'),  # playlist editing keys
            Binding('k100.73:pp_istop.3.127'),
            Binding('k100.75:pp_ianno.3.127'),
            Binding('k100.61:pp_itrans.3.127'),
            Binding('k100.66:pp_ifade.3.127'),
            Binding('k100.6e:pp_ipitch.3.127'),
            Binding('k104.72:pr_on.0.127'),
            Binding('k104.73:ps_on.0.127'),
            Binding('k100.69:pc_tips.0.127'),  # Tooltips shown
        ]
        self.update_lookup()

    def save_prefs(self, where=None):
        """Store bindings list to prefs file
        """
        fp = open((where or PM.basedir) / 'controls', 'w')
        for binding in self.bindings:
            fp.write(str(binding)+'\n')
        fp.close()

    def load_prefs(self):
        """Reload bindings list from prefs file
        """
        cpn = PM.basedir / 'controls'
        if os.path.isfile(cpn):
            fp = open(cpn)
            self.bindings = []
            for line in fp:
                line = line.strip()
                if line != '' and not line.startswith('#'):
                    try:
                        self.bindings.append(Binding(line))
                    except ValueError as e:
                        print('Warning: controls prefs file '
                              'contained unreadable binding %r' % line, file=sys.stderr)
            fp.close()
            self.update_lookup()

    def update_lookup(self):
        """Bindings list has changed, rebuild input lookup
        """
        self.lookup = {}
        for binding in self.bindings:
            self.lookup.setdefault(
                str(binding).split(':', 1)[0], []).append(binding)

    def input(self, input, iv):
        """Dispatch incoming input to all bindings associated with it
        """
        # If a BindingEditor is open in learning mode, inform it of the input
        # instead of doing anything with it.
        #
        if self.learner is not None:
            self.learner.learn(input)
            return

        # Handle input value according to the action mode and pass value with
        # is-delta flag to action methods.
        #
        for binding in self.lookup.get(input, []):
            isd = False
            v = iv
            if binding.mode == Binding.MODE_DIRECT:
                if binding.value < 0:
                    v = 0x7F-v
            else:
                if binding.mode == Binding.MODE_PULSE:
                    if v >= 0x40:
                        if binding in self.repeat_cache:
                            continue
                        else:
                            self.repeat_cache.add(binding)
                    else:
                        self.repeat_cache.discard(binding)

                    if binding.value <= 0x40:
                        v = (~v) & 0x7F  # Act upon release.
                if v < 0x40:
                    continue
                if binding.mode in (Binding.MODE_SET, Binding.MODE_ALTER):
                    v = binding.value
                if binding.mode in (Binding.MODE_PULSE, Binding.MODE_ALTER):
                    isd = True
            # Binding is to be highlighted in the user interface.
            self.highlights[binding] = (3, True)
            getattr(self, binding.method)(binding.target, v, isd)

    def input_key(self, event):
        """Convert incoming key events into input signals
        """
        # Ignore modifier keypresses, suppress keyboard repeat,
        # and include only relevant modifier flags.
        #
        if not(0xFFE1 <= event.keyval < 0xFFEF or 0xFE01 <= event.keyval < 0xFE35):
            state = event.get_state()  # &Binding.MODIFIERS_MASK
            v = 0x7F if event.type == Gdk.EventType.KEY_PRESS else 0
            self.input('k%x.%x' % (state, event.keyval), v)

    # Utility for p_ control methods
    #
    def _get_player(self, n):
        main = self.owner

        if n == 3:
            if main.player_nb.get_current_page() == 1:
                if main.jingles.interlude.treeview.is_focus():
                    n = 2
                else:
                    return None
            elif main.player_left.treeview.is_focus():
                n = 0
            elif main.player_right.treeview.is_focus():
                n = 1
            else:
                return None
        elif n == 4:
            if main.crossfade.get_value() < 50:
                n = 0
            else:
                n = 1

        return (main.player_left, main.player_right, main.jingles.interlude)[n]

    # Control implementations. The @action_method decorator records all control
    # methods in order, so the order they are defined in this code dictates the
    # order they'll appear in in the UI.

    # Miscellaneous
    #
    @action_method(Binding.MODE_PULSE, Binding.MODE_SET)
    def c_tips(self, n, v, isd):
        control = self.owner.prefs_window.enable_tooltips
        if isd:
            v = 0 if control.get_active() else 127
        control.set_active(v >= 64)

    @dbusify(in_signature='b')
    def set_enable_tooltips(self, enabled):
        self.owner.prefs_window.enable_tooltips.set_active(enabled)

    @dbusify(out_signature='b')
    def get_enable_tooltips(self):
        return self.owner.prefs_window.enable_tooltips.get_active()

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT)
    def c_sdjmix(self, n, v, isd):
        active = not self.owner.listen_dj.get_active() if isd else v >= 0x40
        if active:
            self.owner.listen_dj.set_active(True)
        else:
            self.owner.listen_stream.set_active(True)

    @dbusify()
    def set_listen_dj_mix(self):
        self.owner.listen_dj.set_active(True)

    @dbusify(out_signature='b')
    def get_listen_dj_mix(self):
        return self.owner.listen_dj.get_active()

    @dbusify()
    def set_listen_stream_mix(self):
        self.owner.listen_stream.set_active(True)

    @dbusify(out_signature='b')
    def get_listen_stream_mix(self):
        return self.owner.listen_stream.get_active()

    # Panning presets
    #
    @action_method(Binding.MODE_PULSE)
    def l_panpre(self, n, v, isd):
        self.owner.pan_preset_chooser.load_preset(n)

    @dbusify(in_signature='u')
    def panning_presets_load(self, n):
        self.l_panpre(n, 0, 0)

    # Player
    #
    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT)
    def p_pp(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        is_playing = player.is_playing
        if not is_playing and (isd or v >= 0x40):
            player.play.set_active(True)
        if is_playing if isd else (player.is_paused == (v >= 0x40)):
            player.pause.set_active(not player.pause.get_active())

    @dbusify(in_signature='ubb')
    def player_playpause(self, index, play, toggle):
        self.p_pp(index, 127 if play else 0, toggle)

    @action_method(Binding.MODE_PULSE)
    def p_stop(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        player.stop.clicked()

    @dbusify(in_signature='u')
    def player_stop(self, index):
        self.p_stop(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_advance(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        player.advance()

    @dbusify(in_signature='u')
    def player_advance(self, index):
        self.p_advance(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_prev(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        player.prev.clicked()

    @dbusify(in_signature='u')
    def player_previous(self, index):
        self.p_prev(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_next(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        player.next.clicked()

    @dbusify(in_signature='u')
    def player_next(self, index):
        self.p_next(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_sprev(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        treeview_selectprevious(player.treeview)

    @dbusify(in_signature='u')
    def player_select_previous(self, index):
        self.p_sprev(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_snext(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        treeview_selectnext(player.treeview)

    @dbusify(in_signature='u')
    def player_select_next(self, index):
        self.p_snext(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_sfire(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        player.cb_doubleclick(player.treeview, None, None, None)

    @dbusify(in_signature='u')
    def player_play_selected(self, index):
        self.p_sfire(index, 0, 0)

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def p_stream(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        active = not player.stream.get_active() if isd else v >= 0x40
        player.stream.set_active(active)

    @dbusify(in_signature='ubb')
    def player_set_streammix(self, index, value, toggle):
        self.p_stream(index, 127 if value else 0, toggle)

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def p_listen(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        active = not player.listen.get_active() if isd else v >= 0x40
        player.listen.set_active(active)

    @dbusify(in_signature='ubb')
    def player_set_djmix(self, index, value, toggle):
        self.p_listen(index, 127 if value else 0, toggle)

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def p_prep(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        if player not in (self.owner.player_left, self.owner.player_right):
            print("player unsupported for this binding")
            return
        other = self.owner.player_left if player is self.owner.player_right \
            else self.owner.player_right
        prep = player.stream.get_active() if isd else v >= 0x40
        player.stream.set_active(not prep)
        other.listen.set_active(not prep)
        if prep:
            player.listen.set_active(True)
            self.owner.listen_dj.set_active(True)
        else:
            # This is questionable. I like to listen to the Stream output not
            # DJ, so reset to Stream mode after pre-ing. This may not suit
            # everyone. Maybe there should be a different action for preview
            # without returning to stream listening. The alternative would be
            # to try to remember which output was being listened to previously,
            # but that would introduce invisible state not present in the
            # normal UI, making the behaviour unpredictable.
            #
            self.owner.listen_stream.set_active(True)

    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def p_vol(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        if player.playername in ("left", "right"):
            deckadj = self.owner.deck2adj if player is self.owner.player_right \
                else self.owner.deckadj
        elif player.playername == "interlude":
            deckadj = self.owner.jingles.ivol_adj
        cross = deckadj.get_value()+v if isd else v
        deckadj.set_value(cross)

    @dbusify(in_signature='uib')
    def player_set_volume(self, index, value, delta):
        self.p_vol(index, value, delta)

    #@action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    # def p_gain(self, n, v, isd):
    #   player= self._get_player(n)
    #   if player is None: return
    # pass # XXX
    #@action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    # def p_pan(self, n, v, isd):
    #   player= self._get_player(n)
    #   if player is None: return
    # pass # XXX
    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def p_pitch(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        speed = player.pbspeedbar.get_value()+v if isd else v
        player.pbspeedbar.set_value(speed)

    @dbusify(in_signature='uib')
    def player_set_pitch(self, index, value, delta):
        self.p_pitch(index, value, delta)

    # Playlist methods, to reproduce previous idjcmedia shortcuts
    #
    @action_method(Binding.MODE_PULSE)
    def p_tag(self, n, v, isd):  # t
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'MetaTag')

    @action_method(Binding.MODE_PULSE)
    def p_istop(self, n, v, isd):  # s
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Stop Control')

    @dbusify(in_signature='u')
    def playlist_insert_stop_control(self, index):
        self.p_istop(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_istop2(self, n, v, isd):  # s
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Stop Control 2')

    @dbusify(in_signature='u')
    def playlist_insert_stop_control_2(self, index):
        self.p_istop2(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_ianno(self, n, v, isd):  # u
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Announcement Control')

    @action_method(Binding.MODE_PULSE)
    def p_itrans(self, n, v, isd):  # a
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Transfer Control')

    @dbusify(in_signature='u')
    def playlist_insert_transfer_control(self, index):
        self.p_itrans(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_ifade(self, n, v, isd):  # f
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Crossfade Control')

    @dbusify(in_signature='u')
    def playlist_insert_crossfade_control(self, index):
        self.p_ifade(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_ipitch(self, n, v, isd):  # n
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Normal Speed Control')

    @dbusify(in_signature='u')
    def playlist_insert_normal_speed_control(self, index):
        self.p_ipitch(index, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def p_igotop(self, n, v, isd):
        player = self._get_player(n)
        if player is None:
            return
        player.menu_model, player.menu_iter = \
            player.treeview.get_selection().get_selected()
        player.menuitem_response(None, 'Jump To Top Control')

    @dbusify(in_signature='u')
    def playlist_insert_jump_to_top_control(self, index):
        self.p_igotop(index, 0, 0)

    @dbusify(in_signature='us')
    def playlist_insert_pathname(self, index, pathname):
        player = self._get_player(index)
        if player is None:
            return
        model, iter = player.treeview.get_selection().get_selected()
        row = player.get_media_metadata(pathname)
        if row:
            model.insert_after(iter, row)
            self.player_select_next(index)

    @dbusify()
    def playlist_clear(self, index):
        player = self._get_player(index)
        if player is None:
            return
        player.liststore.clear()

    # Both players
    #
    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def x_fade(self, n, v, isd):
        v = v/127.0*100
        cross = self.owner.crossadj.get_value()+v if isd else v
        self.owner.crossadj.set_value(cross)

    @dbusify(in_signature='ib')
    def crossfade_set(self, value, delta):
        self.x_fade(0, value, delta)

    @action_method(Binding.MODE_PULSE)
    def x_advance(self, n, v, isd):
        self.owner.advance.clicked()

    @dbusify()
    def playlist_advance(self):
        self.x_advance(0, 0, 0)

    @action_method(Binding.MODE_PULSE)
    def x_pass(self, n, v, isd):
        self.owner.passbutton.clicked()

    @dbusify()
    def crossfade_pass(self):
        self.x_pass(0, 0, 0)

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def x_pitch(self, n, v, isd):
        checkbox = self.owner.prefs_window.speed_variance
        checkbox.set_active(not checkbox.get_active() if isd else v >= 0x40)

    @dbusify(in_signature='bb')
    def pitch_enable(self, value, toggle):
        self.x_pitch(0, 127 if value else 0, toggle)

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def x_focus(self, n, v, isd):
        if isd:
            if self.owner.player_left.treeview.is_focus():
                player = self.owner.player_right
            else:
                player = self.owner.player_left
        else:
            player = self.owner.player_right if v >= 0x40 else \
                self.owner.player_left
        player.treeview.grab_focus()

    @dbusify(in_signature='ub')
    def main_player_focus(self, index, toggle):
        self.x_focus(0, 127 if index else 0, toggle)

    # Channel
    #
    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def m_on(self, n, v, isd):

        button = self.owner.mic_opener.get_opener_button(n)
        if button is not None:
            s = not button.get_active() if isd else v >= 0x40
            button.set_active(s)

    @dbusify(in_signature='ubb')
    def channel_open(self, index, value, toggle):
        self.m_on(index, 127 if value else 0, toggle)

    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def m_vol(self, n, v, isd):
        agc = getattr(self.owner.prefs_window, 'mic_control_%d' % n)
        vol = agc.valuesdict[agc.commandname+'_gain'].get_adjustment()
        if isd:
            v += vol.props.value
        else:
            v = v / 127.0 * \
                (vol.props.upper - vol.props.lower) + vol.props.lower
        vol.set_value(v)

    @dbusify(in_signature='uib')
    def channel_gain(self, index, value, delta):
        self.m_vol(index, value, delta)

    #@action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    # def m_gain(self, n, v, isd):
    # pass # XXX
    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def m_pan(self, n, v, isd):
        agc = getattr(self.owner.prefs_window, 'mic_control_%d' % n)
        pan = agc.valuesdict[agc.commandname+'_pan']
        v = v/127.0*100
        v = pan.get_value()+v if isd else v
        pan.set_value(v)

    @dbusify(in_signature='uib')
    def channel_pan(self, index, value, delta):
        self.m_pan(index, value, delta)

    # VoIP
    #
    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def v_on(self, n, v, isd):
        phone = self.owner.greenphone
        s = not phone.get_active() if isd else v >= 0x40
        phone.set_active(s)

    @dbusify(in_signature='bb')
    def voip_mode_public(self, value, toggle):
        self.v_on(0, 127 if value else 0, toggle)

    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def v_prep(self, n, v, isd):
        phone = self.owner.redphone
        s = not phone.get_active() if isd else v >= 0x40
        phone.set_active(s)

    @dbusify(in_signature='bb')
    def voip_mode_private(self, value, toggle):
        self.v_prep(0, 127 if value else 0, toggle)

    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def v_vol(self, n, v, isd):
        vol = self.owner.voipgainadj.get_value() + v if isd else v
        self.owner.voipgainadj.set_value(vol)

    @dbusify(in_signature='ib')
    def voip_set_gain(self, value, delta):
        self.v_vol(0, value, delta)

    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def v_mixback(self, n, v, isd):
        vol = self.owner.mixbackadj.get_value() + v if isd else v
        self.owner.mixbackadj.set_value(vol)

    @dbusify(in_signature='ib')
    def voip_set_mixback_level(self, value, delta):
        self.v_mixback(0, value, delta)

    #@action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    # def v_gain(self, n, v, isd):
    # pass # XXX
    #@action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    # def v_pan(self, n, v, isd):
    # pass # XXX
    # One jingle
    #
    @action_method(Binding.MODE_PULSE)
    def k_fire(self, n, v, isd):
        self.owner.jingles.all_effects[n].trigger.clicked()

    @dbusify(in_signature='u')
    def effect_trigger(self, index):
        self.k_fire(index, 0, 0)

    # Jingles player in general
    #
    @action_method(Binding.MODE_PULSE)
    def b_stop(self, n, v, isd):
        if n < 2:
            self.owner.jingles.effect_banks[n].stop()
        else:
            banks = self.owner.jingles.effect_banks
            banks[0].stop()
            if len(banks) > 1:
                banks[1].stop()

    @dbusify(in_signature='bb')
    def effect_bank_stop(self, first, second):
        if first or second:
            self.b_stop(2 if first and second else (0, 1)[second], 0, 0)

    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def b_vol1(self, n, v, isd):
        if n < 2:
            fader = self.owner.jingles.jvol_adj[n]
            vol = fader.get_value()+v if isd else v
            fader.set_value(vol)
        else:
            self.b_vol1(0, v, isd)
            self.b_vol1(1, v, isd)

    @dbusify(in_signature='uib')
    def effect_bank_gain(self, index, value, delta):
        self.b_vol1(index, value, delta)

    @action_method(Binding.MODE_DIRECT, Binding.MODE_SET, Binding.MODE_ALTER)
    def b_vol2(self, n, v, isd):
        if n < 2:
            fader = self.owner.jingles.jmute_adj[n]
            vol = fader.get_value()+v if isd else v
            fader.set_value(vol)
        else:
            self.b_vol2(0, v, isd)
            self.b_vol2(1, v, isd)

    @dbusify(in_signature='uib')
    def effect_bank_headroom(self, index, value, delta):
        self.b_vol2(index, value, delta)

    # Stream connection
    #
    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def s_on(self, n, v, isd):
        connect = self.owner.server_window.streamtabframe.tabs[
            n].server_connect
        s = not connect.get_active() if isd else v >= 0x40
        connect.set_active(s)

    @dbusify(in_signature='ubb')
    def stream_set_connected(self, index, value, toggle):
        self.s_on(index, 127 if value else 0, toggle)

    # Recorder
    #
    @action_method(Binding.MODE_PULSE, Binding.MODE_DIRECT, Binding.MODE_SET)
    def r_on(self, n, v, isd):
        buttons = self.owner.server_window.recordtabframe.tabs[
            n].record_buttons
        s = not buttons.record_button.get_active() if isd else v >= 0x40
        if s:
            buttons.record_button.set_active(s)
        else:
            buttons.stop_button.clicked()

    @dbusify(in_signature='ubb')
    def recorder_set_recording(self, index, value, toggle):
        self.r_on(index, 127 if value else 0, toggle)


# Generic GTK utilities ______________________________________________________

# TreeView move selection up/down with wrapping
#
def treeview_selectprevious(treeview):
    selection = treeview.get_selection()
    model, siter = selection.get_selected()
    iter = model.get_iter_first()
    if iter is not None:
        while True:
            niter = model.iter_next(iter)
            if niter is None or siter is not None and \
                    model.get_path(niter) == model.get_path(siter):
                break
            iter = niter
        selection.select_iter(iter)
        treeview.scroll_to_cell(model.get_path(iter), None, False)


def treeview_selectnext(treeview):
    selection = treeview.get_selection()
    model, siter = selection.get_selected()
    iter = model.get_iter_first()
    if iter is not None:
        if siter is not None:
            siter = model.iter_next(siter)
            if siter is not None:
                iter = siter
        selection.select_iter(iter)
        treeview.scroll_to_cell(model.get_path(iter), None, False)

# Simple value+text-based combo box with optional icon
#


class LookupComboBox(Gtk.ComboBox):

    def __init__(self, values, texts, icons=None):
        self._values = values
        if icons is not None:
            model = Gtk.ListStore(str, bool, GdkPixbuf.Pixbuf)
        else:
            model = Gtk.ListStore(str, bool)
        for valuei, value in enumerate(values):
            if icons is not None:
                model.append((texts[value], True, icons[value]))
            else:
                model.append((texts[value], True))
        super(LookupComboBox, self).__init__()
        self.set_model(model)

        if icons is not None:
            cricon = Gtk.CellRendererPixbuf()
            self.pack_start(cricon, False)
            #self.set_attributes(cricon, pixbuf= 2)
        crtext = Gtk.CellRendererText()
        self.pack_start(crtext, False)
        #self.set_attributes(crtext, text= 0, sensitive= 1)

    def get_value(self):
        active = self.get_active()
        if active == -1:
            active = 0
        return self._values[active]

    def set_value(self, value):
        self.set_active(self._values.index(value))

# Combo box with simple 1-level grouping and insensitive group headings
#


class GroupedComboBox(Gtk.ComboBox):

    def __init__(self, groups, groupnames, values, valuenames, valuegroups):
        self._values = values
        self._lookup = {}
        model = Gtk.TreeStore(int, str, bool)
        group_rows = {}
        for group in groups:
            group_rows[group] = model.append(
                None, [-1, groupnames[group], False])
        for i in range(len(values)):
            iter = model.append(group_rows[valuegroups[i]],
                                [i, valuenames[values[i]], True])
            self._lookup[values[i]] = model.get_path(iter)
        super(GroupedComboBox, self).__init__(model=model)

        cr = Gtk.CellRendererText()
        self.pack_start(cr, True)
        #self.set_attributes(cr, text= 1, sensitive= 2)

    def get_value(self):
        iter = self.get_active_iter()
        if iter is None:
            return self._values[0]
        i = self.get_model().get_value(iter, 0)
        if i == -1:
            return self._values[0]
        return self._values[i]

    def set_value(self, value):
        self.set_active_iter(self.get_model().get_iter(self._lookup[value]))

# Horrible hack to make the text of a SpinButton customisable. If the
# adjustment property is set to a subclass of CustomAdjustment, the display
# text will be customisable through the read_input and write_output method
# of that Adjustment. (With a plain Adjustment, works like normal SpinButton.)
#
# Normally customisation is impossible because the 'input' signal needs an
# output written to its gpointer argument, which is not accessible via PyGTK.
# Try to do the pointer write using ctypes, if available. Otherwise fall back
# to working like a standard ComboBox.
#
try:
    import ctypes
except ImportError:
    ctypes = None


class CustomSpinButton(Gtk.SpinButton):

    def __init__(self, adjustment, climb_rate=0.0, digits=0):
        super(CustomSpinButton, self).__init__(adjustment=adjustment,
                                               climb_rate=climb_rate, digits=digits)
        self._value = adjustment.get_value()
        self._iscustom = ctypes is not None
        if self._iscustom:
            self.connect('input', self._on_input)
            self.connect('output', self._on_output)

    def _on_input(self, _, ptr):
        if not repr(ptr).startswith('<gpointer at 0x'):
            self._iscustom = False
        if not self._iscustom or not isinstance(self.get_adjustment(),
                                                CustomAdjustment):
            return False
        try:
            value = self.get_adjustment().read_input(self.get_text())
        except ValueError:
            value = self._value
        addr = int(repr(ptr)[15:-1], 16)
        ctypes.c_double.from_address(addr).value = float(value)  # danger!
        return True

    def _on_output(self, _):
        if not self._iscustom or not isinstance(self.get_adjustment(),
                                                CustomAdjustment):
            return False
        adj = self.get_adjustment()
        self.set_text(adj.write_output(adj.get_value()))
        return True

    def set_adjustment(self, adjustment):
        v = self.get_adjustment().get_value()
        Gtk.SpinButton.set_adjustment(self, adjustment)
        if v != adjustment.get_value():
            adjustment.set_value(v)
        else:
            adjustment.emit('value-changed')


class CustomAdjustment(Gtk.Adjustment):

    def read_input(self, text):
        return float(text)

    def write_output(self, value):
        if int(value) == value:
            value = int(value)
        return str(value)


# Binding editor popup _______________________________________________________

class BindingEditor(Gtk.Dialog):
    binding_values = {
        # TC: binding editor, action pane, third row, heading text.
        'd': _('Use value'),
        # TC: binding editor, action pane, third row, heading text.
        'p': _('Act if'),
        # TC: binding editor, action pane, third row, heading text.
        's': _('Set to'),
        # TC: binding editor, action pane, third row, heading text.
        'a': _('Adjust by'),
    }

    binding_controls = {
        # TC: binding editor, input pane, fourth row, heading text.
        'c': _('Control'),
        # TC: binding editor, input pane, fourth row, heading text.
        'n': _('Note'),
        # TC: binding editor, input pane, fourth row, heading text.
        'p': _('Control'),
        # TC: binding editor, input pane, fourth row, heading text.
        'k': _('Key'),
    }

    control_method_groups = {
        # TC: binding editor, action pane, first row, toplevel menu.
        'c': _('Miscellaneous'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'p': _('Player'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'x': _('Both players'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'l': _('Quick panning'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'm': _('Channel'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'v': _('VoIP channel'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'k': _('Single effect'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'b': _('Effects bank'),
        # TC: binding editor, action pane, first row, toplevel menu.
        's': _('Stream'),
        # TC: binding editor, action pane, first row, toplevel menu.
        'r': _('Stream recorder'),
    }

    control_modes = {
        # TC: binding editor, action pane, second row, dropdown text.
        'd': _('Direct fader/held button'),
        # TC: binding editor, action pane, second row, dropdown text.
        'p': _('One-shot/toggle button'),
        # TC: binding editor, action pane, second row, dropdown text.
        's': _('Set value'),
        # TC: binding editor, action pane, second row, dropdown text.
        'a': _('Alter value')
    }

    control_sources = {
        # TC: binding editor, input pane, second row, dropdown text.
        'c': _('MIDI control'),
        # TC: binding editor, input pane, second row, dropdown text.
        'n': _('MIDI note'),
        # TC: binding editor, input pane, second row, dropdown text.
        'p': _('MIDI pitch-wheel'),
        # TC: binding editor, input pane, second row, dropdown text.
        'k': _('Keyboard press'),
        # TC: binding editor, input pane, second row, dropdown text.
        'x': _('XChat command')
    }

    def __init__(self, owner):
        self.owner = owner
        super(BindingEditor, self).__init__()
            # TC: Dialog window title text.
            # TC: User is expected to edit a control binding.
        self.set_title(_('Edit control binding'))
        self.set_parent(owner.owner.owner.prefs_window.window)
        self.set_modal(True)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                         Gtk.STOCK_OK, Gtk.ResponseType.OK)

        Gtk.Dialog.set_resizable(self, False)
        owner.owner.owner.window_group.add_window(self)
        self.connect('delete_event', self.on_delete)
        self.connect('close', self.on_close)
        self.connect("key-press-event", self.on_key)

        # Input editing
        #
        # TC: After clicking this button the binding editor will be listening
        # TC: for a key press or midi control surface input.
        self.learn_button = Gtk.ToggleButton(_('Listen for input...'))
        self.learn_button.connect('toggled', self.on_learn_toggled)
        self.learn_timer = None

        self.source_field = LookupComboBox(Binding.SOURCES,
                                           self.control_sources, self.owner.source_icons)
        self.source_field.connect('changed', self.on_source_changed)
        # TC: The input source.
        self.source_label = Gtk.Label(label=_('Source'))

        # TC: The midi channel.
        self.channel_label = Gtk.Label(label=_('Channel'))
        self.channel_field = ModifierSpinButton(ChannelAdjustment())

        self.control_label = Gtk.Label(label=self.binding_controls['c'])
        self.control_field = CustomSpinButton(Gtk.Adjustment(0, 0, 127, 1))

        # Control editing
        #
        self.method_field = GroupedComboBox(
            Binding.METHOD_GROUPS, self.control_method_groups,
            Binding.METHODS, control_methods,
            [m[0] for m in Binding.METHODS]
        )
        self.method_field.connect('changed', self.on_method_changed)

        # TC: The manner in which the input is interpreted.
        self.mode_label = Gtk.Label(label=_('Interaction'))
        self.mode_field = LookupComboBox(Binding.MODES, self.control_modes)
        self.mode_field.connect('changed', self.on_mode_changed)

        # TC: The effect of the control can be directed upon a specific target.
        # TC: e.g. On target [Left player]
        self.target_label = Gtk.Label(label=_('On target'))
        self.target_field = CustomSpinButton(TargetAdjustment('p'))

        self.value_label = Gtk.Label(
            label=self.binding_values[Binding.MODE_SET])
        self.value_field_scale = ValueSnapHScale(0, -127, 127)
        dummy = ValueSnapHScale(0, -127, 127)
        # TC: Checkbutton text.
        # TC: Use reverse scale and invert the meaning of button presses.
        self.value_field_invert = Gtk.CheckButton(_('Reversed'))
        self.value_field_pulse_noinvert = Gtk.RadioButton(
            None, label=_('Pressed'))
        self.value_field_pulse_inverted = Gtk.RadioButton(
            self.value_field_pulse_noinvert, _('Released'))

        # Layout
        #
        for label in (
            self.source_label, self.channel_label, self.control_label,
                self.mode_label, self.target_label, self.value_label):
            label.set_width_chars(10)
            label.set_alignment(0, 0.5)

        sg = Gtk.SizeGroup(Gtk.SizeGroupMode.VERTICAL)

        row0, row1, row2, row3 = Gtk.HBox(spacing=4), Gtk.HBox(spacing=4), \
            Gtk.HBox(spacing=4), Gtk.HBox(spacing=4)
        row0.pack_start(self.learn_button, True, True, 0)
        row1.pack_start(self.source_label, False, False, 0)
        row1.pack_start(self.source_field, True, True, 0)
        row2.pack_start(self.channel_label, False, False, 0)
        row2.pack_start(self.channel_field, True, True, 0)
        row3.pack_start(self.control_label, False, False, 0)
        row3.pack_start(self.control_field, True, True, 0)
        sg.add_widget(row2)

        input_pane = Gtk.VBox(homogeneous=True, spacing=2)
        input_pane.set_border_width(8)
        input_pane.pack_start(row0, False, False, 0)
        input_pane.pack_start(row1, False, False, 0)
        input_pane.pack_start(row2, False, False, 0)
        input_pane.pack_start(row3, False, False, 0)
        input_pane.show_all()

        input_frame = Gtk.Frame(label=" %s " % _('Input'))
        input_frame.set_border_width(4)
        input_frame.add(input_pane)
        input_pane.show()
        set_tip(input_pane, _("The first half of a binding is the input which "
                              "comes in the form of the press of a keyboard key or an event from a "
                              "midi device.\n\nInput selection can be done manually or with the help"
                              " of the '%s' option." % _("Listen for input...")))

        self.value_field_pulsebox = Gtk.HBox()
        self.value_field_pulsebox.pack_start(
            self.value_field_pulse_noinvert, True, True, 0)
        self.value_field_pulsebox.pack_start(
            self.value_field_pulse_inverted, True, True, 0)
        self.value_field_pulsebox.foreach(Gtk.Widget.show)

        sg.add_widget(self.value_field_scale)
        sg.add_widget(self.value_field_invert)
        sg.add_widget(self.value_field_pulsebox)
        sg.add_widget(dummy)
        dummy.show()

        row0, row1, row2, row3 = Gtk.HBox(spacing=4), Gtk.HBox(spacing=4), \
            Gtk.HBox(spacing=4), Gtk.HBox(spacing=4)
        row0.pack_start(self.method_field, True, True, 0)
        row1.pack_start(self.mode_label, False, False, 0)
        row1.pack_start(self.mode_field, True, True, 0)
        row2.pack_start(self.value_label, False, False, 0)
        row2.pack_start(self.value_field_scale, True, True, 0)
        row2.pack_start(self.value_field_invert, True, True, 0)
        row2.pack_start(self.value_field_pulsebox, True, True, 0)
        row3.pack_start(self.target_label, False, False, 0)
        row3.pack_start(self.target_field, True, True, 0)

        action_pane = Gtk.VBox(homogeneous=True, spacing=2)
        action_pane.set_border_width(8)
        action_pane.pack_start(row0, False, False, 0)
        action_pane.pack_start(row1, False, False, 0)
        action_pane.pack_start(row2, False, False, 0)
        action_pane.pack_start(row3, False, False, 0)
        action_pane.show_all()

        action_frame = Gtk.Frame(label=" %s " % _('Action'))
        action_frame.set_border_width(4)
        action_frame.add(action_pane)
        action_pane.show()
        # TC: %s is the translation of 'Action'.
        set_tip(action_pane, _("The '%s' pane determines how the input is "
                               "handled, and to what effect." % _("Action")))

        hbox = Gtk.HBox(True, spacing=4)
        hbox.pack_start(input_frame, True, True, 0)
        hbox.pack_start(action_frame, True, True, 0)
        hbox.show_all()
        self.get_content_area().pack_start(hbox, True, True, 0)
        hbox.show()

    def set_binding(self, binding):
        self.learn_button.set_active(False)
        self.source_field.set_value(binding.source)
        self.channel_field.set_value(binding.channel)
        self.control_field.set_value(binding.control)
        self.method_field.set_value(binding.method)
        self.mode_field.set_value(binding.mode)
        self.target_field.set_value(binding.target)
        self.value_field_scale.set_value(binding.value)
        self.value_field_invert.set_active(binding.value < 64)
        self.value_field_pulse_noinvert.set_active(binding.value >= 64)
        self.value_field_pulse_inverted.set_active(binding.value < 64)

    def get_binding(self):
        mode = self.mode_field.get_value()
        if mode == Binding.MODE_DIRECT:
            value = -127 if self.value_field_invert.get_active() else 127
        elif mode == Binding.MODE_PULSE:
            value = 127 if self.value_field_pulse_noinvert.get_active() else 0
        else:
            value = int(self.value_field_scale.get_value())
        return Binding(
            source=self.source_field.get_value(),
            channel=int(self.channel_field.get_value()),
            control=int(self.control_field.get_value()),
            mode=mode,
            method=self.method_field.get_value(),
            target=int(self.target_field.get_value()),
            value=value
        )

    def on_delete(self, *args):
        self.on_close()
        return True

    def on_close(self, *args):
        self.learn_button.set_active(False)

    def on_key(self, _, event):
        if self.learn_button.get_active():
            self.owner.owner.input_key(event)
            return True
        return False

    # Learn mode, take inputs and set the input fields from them
    #
    def on_learn_toggled(self, *args):
        if self.learn_button.get_active():
            self.learn_button.set_label(_('Listening for input'))
            self.owner.owner.learner = self
        else:
            # TC: Button text. If pressed triggers 'Listening for input' mode.
            self.learn_button.set_label(_('Listen for input...'))
            self.owner.owner.learner = None

    def learn(self, input):
        binding = Binding(input+':dp_pp.0.0')
        self.source_field.set_value(binding.source)
        self.channel_field.set_value(binding.channel)
        self.control_field.set_value(binding.control)
        self.learn_button.set_active(False)

    # Update dependent controls
    #
    def on_source_changed(self, *args):
        s = self.source_field.get_value()

        if s == Binding.SOURCE_KEYBOARD:
            # TC: Refers to key modifiers including Ctrl, Alt, Shift, ....
            self.channel_label.set_text(_('Shifting'))
            self.channel_field.set_adjustment(ModifierAdjustment())
        else:
            # TC: Specifically, the numerical midi channel.
            self.channel_label.set_text(_('Channel'))
            self.channel_field.set_adjustment(ChannelAdjustment())

        self.control_label.set_text(self.binding_controls[s])
        if s == Binding.SOURCE_KEYBOARD:
            self.control_field.set_adjustment(KeyAdjustment())
        elif s == Binding.SOURCE_NOTE:
            self.control_field.set_adjustment(NoteAdjustment())
        else:
            self.control_field.set_adjustment(Gtk.Adjustment(0, 0, 127, 1))
        self.control_label.set_sensitive(s != Binding.SOURCE_PITCHWHEEL)
        self.control_field.set_sensitive(s != Binding.SOURCE_PITCHWHEEL)

    def on_method_changed(self, *args):
        method = self.method_field.get_value()
        modes = getattr(Controls, method).action_modes
        model = self.mode_field.get_model()
        iter = model.get_iter_first()
        i = 0
        while iter is not None:
            model.set_value(iter, 1, Binding.MODES[i] in modes)
            iter = model.iter_next(iter)
            i += 1
        self.mode_field.set_value(modes[0])

        group = method[:1]
        if group == 'p':
            self.target_field.set_adjustment(PlayerAdjustment())
        elif group == 'b':
            self.target_field.set_adjustment(EffectsBankAdjustment())
        elif group in 'mksrl':
            self.target_field.set_adjustment(TargetAdjustment(group))
        else:
            self.target_field.set_adjustment(SingularAdjustment())
        self.target_field.update()

        # Snap state may need altering.
        self.snap_needed = 'p' in modes and 'a' not in modes
        if bool(self.value_field_scale.snap) != self.snap_needed:
            self.mode_field.emit("changed")

    def on_mode_changed(self, *args):
        mode = self.mode_field.get_value()
        self.value_label.set_text(self.binding_values[mode])

        self.value_field_pulsebox.hide()
        self.value_field_scale.hide()
        self.value_field_invert.hide()

        if mode == Binding.MODE_DIRECT:
            self.value_field_invert.set_active(False)
            self.value_field_invert.show()
        elif mode == Binding.MODE_PULSE:
            self.value_field_pulsebox.show()
        else:
            # Find the adjustment limits.
            if mode == Binding.MODE_SET:
                min, max = 0, 127
            else:
                min, max = -127, 127
            val = min + (max - min + 1) // 2
            snap = val if self.snap_needed else None
            self.value_field_scale.set_range(val, min, max, snap)
            self.value_field_scale.show()

# A Compound HScale widget that supports snapping.
#


class ValueSnapHScale(Gtk.HBox):
    can_mark = all(hasattr(Gtk.Scale, x) for x in ('add_mark', 'clear_marks'))

    def __init__(self, *args, **kwds):
        super(ValueSnapHScale, self).__init__()
        self.set_spacing(2)
        self.label = Gtk.Label()
        self.label.set_width_chars(4)
        self.label.set_alignment(1.0, 0.5)
        self.pack_start(self.label, False, False, 0)
        self.hscale = Gtk.HScale()
        self.hscale.connect('change-value', self.on_change_value)
        self.hscale.connect('value-changed', self.on_value_changed)
        # We draw our own value so we can control the alignment.
        self.hscale.set_draw_value(False)
        self.pack_start(self.hscale, True, True, 0)
        self.foreach(Gtk.Widget.show)
        if args:
            self.set_range(*args, **kwds)
        else:
            self.label.set_text("0")
            self.snap = None

    def set_range(self, val, lower, upper, snap=None):
        # Here snap also doubles as the boundary value.
        self.snap = snap
        if snap is not None:
            #policy= Gtk.UPDATE_DISCONTINUOUS
            adj = Gtk.Adjustment(
                val, lower, upper + snap - 1, snap * 2, snap * 2, snap-1)
            adj.connect('notify::value', self.on_value_do_snap, lower, upper)
        else:
            #policy= Gtk.UPDATE_CONTINUOUS
            adj = Gtk.Adjustment(val, lower, upper, 1, 6)
        if self.can_mark:
            self.hscale.clear_marks()
            if not self.snap:
                mark = lower + (upper - lower + 1) // 2
                self.hscale.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        self.hscale.set_adjustment(adj)
        # self.hscale.set_update_policy(policy)
        adj.props.value = val
        self.hscale.emit('value-changed')

    def on_change_value(self, range, scroll, _val):
        if self.snap:
            props = range.get_adjustment().props
            value = props.upper - props.page_size if \
                range.get_value() >= self.snap else props.lower
            self.label.set_text(str(int(value)))

    def on_value_changed(self, range):
        self.label.set_text(str(int(range.get_value())))

    def on_value_do_snap(self, adj, _val, lower, upper):
        val = upper if adj.props.value >= self.snap else lower
        if adj.props.value != val:
            adj.props.value = val
        if val == lower:
            self.snap = lower + (upper - lower) // 4
        else:
            self.snap = lower + (upper - lower) * 3 // 4

    def __getattr__(self, name):
        return getattr(self.hscale, name)

# Extended adjustments for custom SpinButtons
#


class ChannelAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self, value, 0, 15, 1)

    def read_input(self, text):
        return int(text)-1

    def write_output(self, value):
        return str(int(value+1))


class ModifierAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self, value, 0, 127, 1)

    def read_input(self, text):
        return Binding.modifier_to_ord(Binding.str_to_modifier(text))

    def write_output(self, value):
        return Binding.modifier_to_str(Binding.ord_to_modifier(int(value)))


class NoteAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self, value, 0, 127, 1)

    def read_input(self, text):
        return Binding.str_to_note(text)

    def write_output(self, value):
        return Binding.note_to_str(int(value))


class KeyAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self, value, 0, 0xFFFF, 1)

    def read_input(self, text):
        return Binding.str_to_key(text)

    def write_output(self, value):
        return Binding.key_to_str(int(value))


class PlayerAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self, value, 0, 4, 1)

    def read_input(self, text):
        return control_targets_players.index(text)

    def write_output(self, value):
        return control_targets_players[max(min(int(value), 4), 0)]


class EffectsBankAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self, value, 0, 2, 1)

    def read_input(self, text):
        return control_targets_effects_bank.index(text)

    def write_output(self, value):
        return control_targets_effects_bank[max(min(int(value), 2), 0)]


class TargetAdjustment(CustomAdjustment):

    def __init__(self, group, value=0):
        CustomAdjustment.__init__(self, value, 0, {
            'p': 3, 'm': 11, 'k': 23, 's': 8, 'r': 3, 'l': 8}
            [group], 1)
        self._group = group

    def read_input(self, text):
        return int(text.rsplit(' ', 1)[-1])-1

    def write_output(self, value):
        return '%s %d' % (control_targets[self._group], value+1)


class SingularAdjustment(CustomAdjustment):

    def __init__(self, value=0):
        CustomAdjustment.__init__(self)

    def read_input(self, text):
        return 0.0

    def write_output(self, value):
        return _('Singular control')

# SpinButton that can translate its underlying adjustment values to GTK shift
# key modifier flags, when a ModifierAdjustment is used.
#


class ModifierSpinButton(CustomSpinButton):

    def get_value(self):
        value = CustomSpinButton.get_value(self)
        if isinstance(self.get_adjustment(), ModifierAdjustment):
            value = Binding.ord_to_modifier(int(value))
        return value

    def set_value(self, value):
        if isinstance(self.get_adjustment(), ModifierAdjustment):
            value = Binding.modifier_to_ord(int(value))
        CustomSpinButton.set_value(self, value)


# Main UI binding list tab ___________________________________________________

class ControlsUI(Gtk.VBox):

    """Controls main config interface, displayed in a tab by IDJCmixprefs
    """
    tooltip_coords = (0, 0)

    def __init__(self, owner):
        super(ControlsUI, self).__init__(spacing=4)
        self.owner = owner

        self.source_icons = {}
        for ct in Binding.SOURCES:
            self.source_icons[ct] = GdkPixbuf.Pixbuf.new_from_file(
                FGlobs.pkgdatadir / ('control_' + ct + ".png"))
        self.editor = BindingEditor(self)
        self.editor.connect('response', self.on_editor_response)
        self.editing = None

        # Control list
        #
        # TC: Tree column heading for Inputs e.g. Backspace, F1, S.
        column_input = Gtk.TreeViewColumn(_('Input'))
        column_input.set_expand(True)
        cricon = Gtk.CellRendererPixbuf()
        crtext = Gtk.CellRendererText()
        crtext.props.ellipsize = Pango.EllipsizeMode.END
        column_input.pack_start(cricon, False)
        column_input.pack_start(crtext, True)
        column_input.set_attributes(cricon, pixbuf=3, cell_background=8)
        column_input.set_attributes(crtext, text=4)
        column_input.set_sort_column_id(0)
        craction = Gtk.CellRendererText()
        crmodifier = Gtk.CellRendererText()
        crmodifier.props.xalign = 1.0
        # TC: Tree column heading for actions e.g. Player stop.
        column_action = Gtk.TreeViewColumn(_('Action'))
        column_action.pack_start(craction, True)
        column_action.pack_start(crmodifier, False)
        column_action.set_attributes(craction, text=5)
        column_action.set_attributes(crmodifier, text=6)
        column_action.set_sort_column_id(1)
        column_action.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        # TC: Tree column heading for targets e.g. Channel 1, Stream 2
        column_target = Gtk.TreeViewColumn(
            _('Target'), Gtk.CellRendererText(), text=7)
        column_target.set_sort_column_id(2)

        model = BindingListModel(self)
        model_sort = Gtk.TreeModelSort(model)
        model_sort.set_sort_column_id(2, Gtk.SortType.ASCENDING)
        self.tree = Gtk.TreeView(model_sort)
        self.tree.connect('realize', model.on_realize,
                          column_input, model_sort)
        self.tree.connect('cursor-changed', self.on_cursor_changed)
        self.tree.connect('key-press-event', self.on_tree_key)
        self.tree.connect('query-tooltip', self.on_tooltip_query)
        model.connect('row-deleted', self.on_cursor_changed)
        self.tree.append_column(column_input)
        self.tree.append_column(column_action)
        self.tree.append_column(column_target)
        self.tree.set_headers_visible(True)
        self.tree.set_rules_hint(True)
        self.tree.set_enable_search(False)
        self.tree.set_has_tooltip(True)

        # New/Edit/Remove buttons
        #
        # TC: User to create a new input binding.
        self.new_button = Gtk.Button(stock=Gtk.STOCK_NEW)
        # TC: User to remove an input binding.
        self.remove_button = Gtk.Button(stock=Gtk.STOCK_DELETE)
        # TC: User to modify an existing input binding.
        self.edit_button = Gtk.Button(stock=Gtk.STOCK_EDIT)
        self.new_button.connect('clicked', self.on_new)
        self.remove_button.connect('clicked', self.on_remove)
        self.edit_button.connect('clicked', self.on_edit)
        self.tree.connect('row-activated', self.on_edit)

        # Layout
        #
        buttons = Gtk.HButtonBox()
        buttons.set_spacing(8)
        buttons.set_layout(Gtk.ButtonBoxStyle.END)
        buttons.pack_start(self.edit_button, False, False, 0)
        buttons.pack_start(self.remove_button, False, False, 0)
        buttons.pack_start(self.new_button, False, False, 0)
        buttons.show_all()
        self.on_cursor_changed()

        self.set_border_width(4)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree)
        self.pack_start(scroll, True, True, 0)
        self.pack_start(buttons, False, False, 0)
        self.show_all()

    # Dynamic tooltip generation
    #
    def on_tooltip_query(self, tv, x, y, kb_mode, tooltip):
        if (x, y) != self.tooltip_coords:
            self.tooltip_coords = (x, y)
        elif None not in (x, y) and \
                self.owner.owner.prefs_window.enable_tooltips.get_active():
            path = tv.get_path_at_pos(
                *tv.convert_widget_to_bin_window_coords(x, y))
            if path is not None:
                row = tv.get_model()[path[0]]
                hbox = Gtk.HBox()
                hbox.set_spacing(3)
                hbox.pack_start(
                    Gtk.Image.new_from_pixbuf(row[3].copy()), False, True, 0)
                hbox.pack_start(Gtk.Label(row[4]), False, True, 0)
                hbox.pack_start(
                    Gtk.Label("  " + row[5] + row[6]), False, False, 0)
                if row[7]:
                    hbox.pack_start(Gtk.Label("  " + row[7]), False, False, 0)
                hbox.show_all()
                tooltip.set_custom(hbox)
                return True

    # Tree interaction
    #
    def on_cursor_changed(self, *args):
        isselected = self.tree.get_selection().count_selected_rows() != 0
        self.edit_button.set_sensitive(isselected)
        self.remove_button.set_sensitive(isselected)

    def on_tree_key(self, tree, event, *args):
        if event.keyval == 0xFFFF:  # GDK_Delete
            self.on_remove()

    # Button presses
    #
    def on_remove(self, *args):
        model_sort, iter_sort = self.tree.get_selection().get_selected()
        model = model_sort.get_model()
        if iter_sort is None:
            return
        iter = model_sort.convert_iter_to_child_iter(None, iter_sort)
        binding = self.owner.bindings[model.get_path(iter)[0]]

        if binding is self.editing:
            self.editor.learnbutton.set_active(False)
            self.editor.hide()
            self.editing = None
        niter = model.iter_next(iter)
        if niter is None:
            treeview_selectprevious(self.tree)
        else:
            treeview_selectnext(self.tree)
        model.remove(iter)
        self.on_cursor_changed()

    def on_new(self, *args):
        model_sort, iter_sort = self.tree.get_selection().get_selected()
        model = model_sort.get_model()
        if iter_sort is not None:
            iter = model_sort.convert_iter_to_child_iter(None, iter_sort)
            binding = self.owner.bindings[model.get_path(iter)[0]]
        else:
            binding = Binding()

        self.editing = None
        self.editor.set_binding(binding)
        self.editor.show()

    def on_edit(self, *args):
        model_sort, iter_sort = self.tree.get_selection().get_selected()
        if iter_sort is None:
            return
        model = model_sort.get_model()
        iter = model_sort.convert_iter_to_child_iter(None, iter_sort)

        self.editing = iter
        self.editor.set_binding(self.owner.bindings[model.get_path(iter)[0]])
        self.editor.show()

    def on_editor_response(self, _, response):
        if response == Gtk.ResponseType.OK:
            model = self.tree.get_model().get_model()
            binding = self.editor.get_binding()
            if self.editing == None:
                path = model.append(binding)
            else:
                path = model.replace(self.editing, binding)
            path_sort = self.tree.get_model().convert_child_path_to_path(path)
            self.tree.get_selection().select_path(path_sort)
            self.tree.scroll_to_cell(path_sort, None, False)
            self.on_cursor_changed()
        self.editor.hide()


class BindingListModel(GenericTreeModel):

    """TreeModel mapping the list of Bindings in Controls to a TreeView
    """

    def __init__(self, owner):
        super(BindingListModel, self).__init__()
        self.owner = owner
        self.bindings = owner.owner.bindings
        self.highlights = owner.owner.highlights

    def on_realize(self, tree, column0, model_sort):
        source = timeout_add(100, self.cb_highlights,
                             tree, column0, model_sort)
        tree.connect_object('destroy', source_remove, source)

    @threadslock
    def cb_highlights(self, tree, column0, model_sort):
        d = self.highlights
        if d:
            for rowref, (count, is_new) in list(d.items()):
                # Highlights counter is reduced.
                if count < 1:
                    del d[rowref]
                else:
                    d[rowref] = (count - 1, False)
                # TreeView area invalidation to trigger a redraw.
                if is_new or rowref not in d:
                    try:
                        path = self.on_get_path(rowref)
                    except ValueError:
                        # User craftily deleted the entry during highlighting.
                        pass
                    else:
                        path = model_sort.convert_child_path_to_path(path)
                        area = tree.get_background_area(path, column0)
                        tree.get_bin_window().invalidate_rect(area, False)
        return True

    def on_get_flags(self):
        return Gtk.TreeModelFlags.LIST_ONLY | Gtk.TreeModelFlags.ITERS_PERSIST

    def on_get_n_columns(self):
        return len(BindingListModel.column_types)

    def on_get_column_type(self, index):
        return BindingListModel.column_types[index]

    def has_default_sort_func(self):
        return False

    # Pure-list iteration
    #
    def on_get_iter(self, path):
        return self.bindings[path[0]] if self.bindings else None

    def on_get_path(self, rowref):
        return (self.bindings.index(rowref),)

    def on_iter_next(self, rowref):
        i = self.bindings.index(rowref)+1
        if i >= len(self.bindings):
            return None
        return self.bindings[i]

    def on_iter_children(self, rowref):
        if rowref is None and len(self.bindings) >= 1:
            return self.bindings[0]
        return None

    def on_iter_has_child(self, rowref):
        return False

    def on_iter_n_children(self, rowref):
        if rowref is None:
            return len(self.bindings)
        return 0

    def on_iter_nth_child(self, rowref, i):
        if rowref is None and i < len(self.bindings):
            return self.bindings[i]
        return None

    def on_iter_parent(self, child):
        return None

    # Make column data from binding objects
    #
    column_types = [str, str, str, GdkPixbuf.Pixbuf, str, str, str, str, str]

    def on_get_value(self, binding, i):
        if i < 3:  # invisible sort columns
            inputix = '%02x.%02x.%04x' % (Binding.SOURCES.index(binding.source),
                                          binding.channel, binding.control)
            methodix = '%02x' % Binding.METHODS.index(binding.method)
            targetix = '%02x.%02x' % (Binding.METHOD_GROUPS.index(
                binding.method[0]), binding.target)
            return ':'.join(((inputix, methodix, targetix),
                             (methodix, targetix, inputix), (targetix, methodix, inputix))[i])

        elif i == 3:  # icon column
            return self.owner.source_icons[binding.source]
        elif i == 4:  # input channel/control column
            return binding.input_str
        elif i == 5:  # method column
            return binding.action_str
        elif i == 6:  # mode/value column
            return binding.modifier_str
        elif i == 7:  # target column
            return binding.target_str
        elif i == 8:  # background color column
            return "red" if binding in self.highlights else None

    # Alteration
    #
    def remove(self, iter):
        path = self.get_path(iter)
        del self.bindings[path[0]]
        self.row_deleted(path)
        self.owner.owner.update_lookup()

    def append(self, binding):
        path = (len(self.bindings),)
        self.bindings.append(binding)
        iter = self.get_iter(path)
        self.row_inserted(path, iter)
        self.owner.owner.update_lookup()
        return path

    def replace(self, iter, binding):
        path = self.get_path(iter)
        del self.bindings[path[0]]
        self.row_deleted(path)
        self.bindings.insert(path[0], binding)
        iter = self.get_iter(path)
        self.row_inserted(path, iter)
        self.owner.owner.update_lookup()
        return path
