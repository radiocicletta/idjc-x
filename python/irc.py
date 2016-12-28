"""IRC bots for IDJC."""

#   Copyright (C) 2011, 2012
#   Stephen Fairchild (s-fairchild@users.sourceforge.net)
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


import re
import json
import time
import sys
import threading
import traceback
import gettext
from inspect import getargspec
from functools import wraps, partial

from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Pango

try:
    from irc import client
    from irc import events
except ImportError:
    traceback.print_exc()
    print("No IRC support")
    HAVE_IRC = False
else:
    HAVE_IRC = True

from idjc import FGlobs
from idjc.prelims import ProfileManager
from .gtkstuff import DefaultEntry
from .gtkstuff import NamedTreeRowReference
from .gtkstuff import ConfirmationDialog
from .gtkstuff import threadslock, gdklock
from .gtkstuff import timeout_add, source_remove
from .utils import string_multireplace
from .tooltips import set_tip

__all__ = ["IRCPane"]

_ = gettext.translation(FGlobs.package_name, FGlobs.localedir,
                        fallback=True).gettext

XCHAT_COLOR = {
    0:  0xCCCCCCFF,
    1:  0x000000FF,
    2:  0x3636B2FF,
    3:  0x2A8C2AFF,
    4:  0xC33B3BFF,
    5:  0xC73232FF,
    6:  0x80267FFF,
    7:  0x66361FFF,
    8:  0xD9A641FF,
    9:  0x3DCC3DFF,
    10: 0x1A5555FF,
    11: 0x2F8C74FF,
    12: 0x4545E6FF,
    13: 0xB037B0FF,
    14: 0x4C4C4CFF,
    15: 0x959595FF
}

MESSAGE_CATEGORIES = (
    # TC: IRC message subcategory, triggers on new track announcements.
    _("Track announce"),
    # TC: IRC message subcategory, triggered by a timer.
    _("Timer"),
    # TC: IRC message subcategory, triggered once when the stream starts.
    _("On stream up"),
    # TC: IRC message subcategory, triggered once at the stream's end.
    _("On stream down"),
    # TC: IRC message subcategory, triggered once at the stream's end.
    _("Operations"))

ASCII_C0 = "".join(chr(x) for x in range(32))

CODES_AND_DESCRIPTIONS = list(
    zip(("%r", "%t", "%l", "%s", "%n", "%d", "%u", "%U"),
        (_('Artist'), _('Title'), _('Album'), _('Song name'),
         _('DJ name'), _('Description'), _('Listen URL'), _('Source URI'))))


class IRCEntry(Gtk.Entry):  # pylint: disable=R0904

    """Specialised IRC text entry widget.

    Features pop-up menu and direct control character insertion.
    """

    _control_keytable = {107: "\u0003", 98: "\u0002",
                         117: "\u001F", 111: "\u000F"}

    def __init__(self, *args, **kwds):
        super(IRCEntry, self).__init__(*args, **kwds)
        self.connect("key-press-event", self._on_key_press_event)
        self.connect("populate-popup", self._popup_menu_populate)

    def _on_key_press_event(self, entry, event):
        """Handle direct insertion of control characters."""

        if entry.im_context_filter_keypress(event):
            return True

        # Check for CTRL key modifier.
        if event.get_state() & Gdk.ModifierType.CONTROL_MASK:
            # Remove the effect of CAPS lock - works for letter keys only.
            keyval = event.keyval + (
                32 if event.get_state() & Gdk.ModifierType.LOCK_MASK else 0)
            try:
                replacement = self._control_keytable[keyval]
            except KeyError:
                pass
            else:
                entry.reset_im_context()
                cursor = entry.get_position()
                entry.insert_text(replacement, cursor)
                entry.set_position(cursor + 1)
                return True

    def _popup_menu_populate(self, entry, menu):
        """Builds the right click pop-up menu on the IRCEntry widget."""

        # TC: Popup menu item for a GTK text entry widget.
        menuitem = Gtk.MenuItem(_('Insert Attribute or Colour Code'))
        menu.append(menuitem)
        submenu = Gtk.Menu()
        menuitem.set_submenu(submenu)
        menuitem.show()

        self._popup_menu_add_substitutions(entry, submenu)
        self._popup_menu_add_colourselectors(entry, submenu)

    def _popup_menu_add_substitutions(self, entry, submenu):
        """Adder for menu items that insert substitute characters or codes."""

        def sub(pairs):
            """Build the attribute inserting menu elements."""

            for code, menutext in pairs:
                menuitem = Gtk.MenuItem()
                label = Gtk.Label()
                label.set_alignment(0.0, 0.5)
                label.set_markup(menutext)
                menuitem.add(label)
                label.show()
                menuitem.connect_object(
                    "activate", self._on_menu_item_activate,
                    entry, code)
                submenu.append(menuitem)
                menuitem.show()

        sub(CODES_AND_DESCRIPTIONS)

        # Separate data tokens from formatting tokens.
        sep = Gtk.SeparatorMenuItem()
        submenu.append(sep)
        sep.show()

        sub(list(zip(("\u0002", "\u001F", "\u000F"), (
            # TC: Text formatting style.
            _('<b>Bold</b>'),
            # TC: Text formatting style.
            _('<u>Underline</u>'),
            # TC: Text formatting style.
            _('Normal')))))

    def _popup_menu_add_colourselectors(self, entry, submenu):
        """Adder for menuitems that choose text colour."""

        for lower, upper in ((0, 7), (8, 15)):
            menuitem = Gtk.MenuItem(_("Colours") + " %d-%d" % (lower, upper))
            submenu.append(menuitem)
            colourmenu = Gtk.Menu()
            menuitem.set_submenu(colourmenu)
            colourmenu.show()
            for i in range(lower, upper + 1):
                try:
                    rgba = XCHAT_COLOR[i]
                except (IndexError, TypeError):
                    continue

                colourmenuitem = Gtk.MenuItem()
                colourmenuitem.connect_object(
                    "activate",
                    self._on_menu_insert_colour_code, entry,
                    i)
                hbox = Gtk.HBox()

                label = Gtk.Label()
                label.set_alignment(0, 0.5)
                label.set_markup(
                    "<span font_family='monospace'>%02d</span>" % i)
                hbox.pack_start(label, True, True, 0)
                label.show()

                pixbuf = GdkPixbuf.Pixbuf(
                    GdkPixbuf.Colorspace.RGB, True, 8, 20, 20)
                pixbuf.fill(rgba)
                image = Gtk.image_new_from_pixbuf(pixbuf)
                image.connect_after(
                    "expose-event",
                    lambda w, e: self._on_colour_box_expose(w))
                hbox.pack_start(image, True, True, 0)
                image.show()

                colourmenuitem.add(hbox)
                hbox.show()
                colourmenu.append(colourmenuitem)
                colourmenuitem.show()
            menuitem.show()

    @staticmethod
    def _on_menu_item_activate(entry, code):
        """Perform relevant character code insertion."""

        cursor = entry.get_position()
        entry.insert_text(code, cursor)
        entry.set_position(cursor + len(code))

    @staticmethod
    def _on_menu_insert_colour_code(entry, code):
        """Insert the colour palette control code."""

        cursor = entry.get_position()
        if cursor < 3 or entry.get_text()[cursor - 3] != "\x03":
            # Foreground colour.
            entry.insert_text("\u0003" + str("%02d" % code), cursor)
        else:
            # Background colour.
            entry.insert_text(str(",%02d" % code), cursor)
        entry.set_position(cursor + 3)

    @staticmethod
    def _on_colour_box_expose(widget):
        """If we are here the mouse is hovering over a colour palette item.

        This causes pre-light which messes up the colour so all we do here
        is cancel it.
        """

        widget.set_state(Gtk.StateType.NORMAL)


class IRCView(Gtk.TextView):  # pylint: disable=R0904

    """A viewer for IRC text.

    This text window shows the text as it would be displayed to other users.
    Variables are substituted for human readable place markers.
    """

    matches = tuple((a, re.compile(b)) for a, b in (
        ("foreground_background", "\x03[0-9]{1,2},[0-9]{1,2}"),
        ("foreground",  "\x03[0-9]{1,2}(?!=,)"),
        ("bold", "\x02"),
        ("underline",  "\x1F"),
        ("normal", "\x0F"),
        ("text", "[^\x00-\x1F]*"),)
    )

    readable_equiv = tuple((x, "<%s>" % y) for x, y in CODES_AND_DESCRIPTIONS)

    def __init__(self):
        super(IRCView, self).__init__()
        self.set_size_request(500, -1)
        self.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.set_editable(False)
        self.set_cursor_visible(False)
        self._rslt = self._foreground = self._background = None
        self._bold = self._underline = False

    def set_text(self, text):
        """Apply text to the viewer.

        IRC text formatting is handled and the view updated.
        """

        text = string_multireplace(text, self.readable_equiv)

        buf = self.get_buffer()
        buf.remove_all_tags(buf.get_start_iter(), buf.get_end_iter())
        buf.delete(buf.get_start_iter(), buf.get_end_iter())

        start = 0

        while start < len(text):
            for name, match in self.matches:
                self._rslt = match.match(text, start)
                if self._rslt is not None and self._rslt.group():
                    # Execute the handler routine.
                    getattr(self, "_handle_" + name)()

                    start = self._rslt.end()
                    break
            else:
                start += 1

        self._foreground = self._background = None
        self._bold = self._underline = False

    @staticmethod
    def _colour_string(code):
        """The colour as a string of format "#rrggbb.

        rgb = red, green, blue as a 2 digit hex number."""

        return "#%06X" % (XCHAT_COLOR[int(code)] >> 8)

    def _handle_bold(self):
        """Bold toggle."""

        self._bold = not self._bold

    def _handle_underline(self):
        """Underline toggle."""

        self._underline = not self._underline

    def _handle_foreground(self):
        """Foreground colour setting."""

        try:
            self._foreground = self._rslt.group()[1:]
        except IndexError:
            self._foreground = None

    def _handle_foreground_background(self):
        """Foreground and background colour setting."""

        try:
            self._foreground, self._background = \
                self._rslt.group()[1:].split(",")
        except IndexError:
            self._foreground = self._background = None

    def _handle_normal(self):
        """The normal formatting tag."""

        self._bold = self._underline = False
        self._foreground = self._background = None

    def _handle_text(self):
        """Normal printable text."""

        buf = self.get_buffer()
        tag = buf.create_tag()
        props = tag.props
        props.family = "monospace"
        try:
            props.foreground = self._colour_string(self._foreground)
            props.background = self._colour_string(self._background)
        except (TypeError, KeyError):
            pass

        if self._underline:
            props.underline = Pango.Underline.SINGLE
        if self._bold:
            props.weight = Pango.Weight.BOLD

        buf.insert_with_tags(
            buf.get_end_iter(),
            elf._rslt.group(),
            tag)


class EditDialogMixin(object):

    """Mix-in class to convert initial-data-entry dialogs to edit dialogs."""

    def __init__(self, orig_data):
        bb = self.get_action_area()
        self.refresh = Gtk.Button(Gtk.STOCK_REFRESH)
        self.refresh.set_use_stock(True)
        self.refresh.connect("clicked", lambda w: self.from_tuple(orig_data))
        bb.add(self.refresh)
        bb.set_child_secondary(self.refresh, True)
        self.refresh.clicked()
        self.delete = Gtk.Button(stock=Gtk.STOCK_DELETE)
        bb.add(self.delete)

    def delete_confirmation(self, deleter):
        """Override in subclass to install a confirmation dialog.

        In this case the deleter function is run without question.
        """

        return deleter


server_port_adj = Gtk.Adjustment(6667.0, 0.0, 65535.0, 1.0, 10.0)


class ServerDialog(Gtk.Dialog):

    """Data entry dialog for adding a new IRC server."""

    optinfo = _("Optional data entry field for information only.")

    # TC: Tab heading text.
    def __init__(self, title=_("IRC server")):
        super(ServerDialog, self).__init__()
        self.set_title(title + " - IDJC" + ProfileManager().title_extra)

        self.network = Gtk.Entry()
        set_tip(self.network, self.optinfo)
        self.network.set_width_chars(25)
        self.hostname = Gtk.Entry()
        self.port = Gtk.SpinButton(server_port_adj)
        self.username = Gtk.Entry()
        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.manual_start = Gtk.CheckButton(_("Manual start"))
        set_tip(
            self.manual_start,
            _('Off when restarting IDJC and off initially.')
        )
        self.nick1 = Gtk.Entry()
        self.nick2 = Gtk.Entry()
        self.nick3 = Gtk.Entry()
        self.realname = Gtk.Entry()
        self.nickserv = Gtk.Entry()
        self.nickserv.set_visibility(False)

        hbox = Gtk.HBox()
        hbox.set_border_width(16)
        hbox.set_spacing(5)

        image = Gtk.Image.new_from_stock(
            Gtk.STOCK_NETWORK, Gtk.IconSize.DIALOG)
        image.set_alignment(0.5, 0)
        table = Gtk.Table(10, 2)
        table.set_col_spacings(6)
        table.set_row_spacings(3)
        rvbox = Gtk.VBox(True)
        hbox.pack_start(image, False, padding=20)
        hbox.pack_start(table, True)

        for i, (text, widget) in enumerate(zip((
            # TC: The IRC network e.g. EFnet.
            _("Network"),
            # TC: label for hostname entry.
            _("Hostname"),
            # TC: TCP/IP port number label.
            _("Port"),
            _("User name"),
            _("Password"), "",
            # TC: IRC nickname data entry label.
            _("Nickname"),
            # TC: Second choice of IRC nickname.
            _("Second choice"),
            # TC: Third choice of IRC nickname.
            _("Third choice"),
            # TC: The IRC user's 'real' name.
            _("Real name"),
            # TC: The NickServ password.
            _("NickServ p/w")),
                (self.network, self.hostname, self.port,
                 self.username, self.password, self.manual_start, self.nick1,
                 self.nick2, self.nick3, self.realname, self.nickserv))):
            # TC: Tooltip to IRC 'User name' field.
            set_tip(
                self.username,
                _("Ideally set this to something even on "
                  "servers that allow public anonymous access."))
            l = Gtk.Label(label=text)
            l.set_alignment(1.0, 0.5)

            table.attach(
                l, 0, 1, i, i + 1,
                Gtk.AttachOptions.SHRINK | Gtk.AttachOptions.FILL)
            table.attach(widget, 1, 2, i, i + 1)

        for each in (self.nick1, self.nick2, self.nick3):
            # TC: tooltip to all IRC nicknames entry fields.
            set_tip(
                each,
                _("When a nickname is in use on the target IRC "
                  "network, during connection these IRC nicknames "
                  "are cycled through, then twice again after "
                  "appending an additional underscore until "
                  "giving up. This gives IDJC a maximum of "
                  "nine IRC nicknames to try."))
        set_tip(
            self.realname,
            _("The real name you want to use which will be "
              "available regardless of whether the network "
              "connection was made with the primary nickname "
              "or not.\n\nIdeally set this to something."))
        set_tip(
            self.nickserv,
            _("If this value is set an attempt will be made "
              "to acquire your first choice IRC nickname "
              "(if needed) and log in with NickServ@services."
              "\n\nThe use of the NickServ service requires prior "
              "nickname registration on the network using a "
              "regular chat client."))

        self.get_content_area().add(hbox)

    def as_tuple(self):
        """Data extraction method."""

        return (
            self.manual_start.get_active(),
            self.port.get_value(),
            False,
            self.network.get_text().strip(),
            self.hostname.get_text().strip(),
            self.username.get_text().strip(),
            self.password.get_text().strip(),
            self.nick1.get_text().strip(),
            self.nick2.get_text().strip(),
            self.nick3.get_text().strip(),
            self.realname.get_text().strip(),
            self.nickserv.get_text().strip()
        )


class EditServerDialog(ServerDialog, EditDialogMixin):

    """Adds a delete and restore button to the standard server dialog."""

    def __init__(self, orig_data):
        ServerDialog.__init__(self)
        EditDialogMixin.__init__(self, orig_data)

    def delete_confirmation(self, deleter):
        def inner(w):
            cd = ConfirmationDialog(
                "",
                _("<span weight='bold' size='12000'>"
                  "Permanently delete this server?</span>\n\n"
                  "This action will also "
                  "erase all of its associated messages."),
                markup=True)
            cd.set_transient_for(self)
            cd.ok.connect("clicked", deleter)
            cd.show_all()

        return inner

    def from_tuple(self, orig_data):
        """The data restore method."""

        n = iter(orig_data).__next__
        self.manual_start.set_active(n())
        self.port.set_value(n())
        n()
        self.network.set_text(n())
        self.hostname.set_text(n())
        self.username.set_text(n())
        self.password.set_text(n())
        self.nick1.set_text(n())
        self.nick2.set_text(n())
        self.nick3.set_text(n())
        self.realname.set_text(n())
        self.nickserv.set_text(n())


message_delay_adj = Gtk.Adjustment(10, 0, 30, 1, 10)
message_offset_adj = Gtk.Adjustment(0, 0, 9999, 1, 10)
message_interval_adj = Gtk.Adjustment(600, 60, 9999, 1, 10)


class ChannelsDialog(Gtk.Dialog):

    """Channels entry dialog."""

    icon = Gtk.STOCK_NEW
    title = "missing title"

    def __init__(self, title=None):
        if title is None:
            title = self.title

        super(ChannelsDialog, self).__init__()
        self.set_title(title + " - IDJC" + ProfileManager().title_extra)

        chbox = Gtk.HBox()
        chbox.set_spacing(6)
        # TC: An IRC channel #chan or user name entry box label.
        l = Gtk.Label(label=_("Channels/Users"))
        self.channels = Gtk.Entry()
        chbox.pack_start(l, False)
        chbox.pack_start(self.channels, True)
        set_tip(
            self.channels,
            _("The comma or space separated list of channels"
              " and/or users to whom the message will be sent.\n\n"
              "Protected channels are included with the form:\n"
              "#channel:keyword."))

        self.mainbox = Gtk.VBox()
        self.mainbox.set_spacing(5)
        self.mainbox.pack_start(chbox, False)

        self.hbox = Gtk.HBox()
        self.hbox.set_border_width(16)
        self.hbox.set_spacing(5)
        self.image = Gtk.Image.new_from_stock(self.icon, Gtk.IconSize.DIALOG)
        self.image.set_alignment(0.5, 0)
        self.hbox.pack_start(self.image, False, padding=20)
        self.hbox.pack_start(self.mainbox, True, True, 0)

        self.get_content_area().add(self.hbox)
        self.channels.grab_focus()

    def _from_channels(self):
        text = self.channels.get_text().replace(",", " ").split()
        return ",".join(x for x in text if x)

    def as_tuple(self):
        """Data extraction method."""

        return (self._from_channels(),)


class EditChannelsDialog(ChannelsDialog, EditDialogMixin):

    """Adds delete and restore buttons to a channels dialog."""

    icon = Gtk.STOCK_EDIT

    def __init__(self, title, orig_data):
        ChannelsDialog.__init__(self, title)
        EditDialogMixin.__init__(self, orig_data)

    def from_tuple(self, orig_data):
        """The data restore method."""

        self.channels.set_text(orig_data[0])


class MessageDialog(ChannelsDialog):

    """Message entry dialog."""

    def __init__(self, title=None):
        ChannelsDialog.__init__(self, title)

        hbox = Gtk.HBox()
        hbox.set_spacing(6)
        # TC: Message text to send to an IRC channel. Widget label.
        l = Gtk.Label(label=_("Message"))
        self.message = IRCEntry()
        hbox.pack_start(l, False)
        hbox.pack_start(self.message, True, True, 0)
        set_tip(
            self.message,
            _("The message to send.\n\nOn the pop-up window "
              "(mouse right click) are some useful options "
              "for embedding metadata and for text formatting."
              "\n\nThe window below displays how the message "
              "will appear to users of XChat."))
        self.mainbox.pack_start(hbox, False)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
        irc_view = IRCView()
        sw.add(irc_view)
        self.mainbox.pack_start(sw, False)

        self.message.connect(
            "changed",
            lambda w: irc_view.set_text(w.get_text()))

    def _pack(self, widgets):
        vbox = Gtk.VBox()
        for l, w in widgets:
            ivbox = Gtk.VBox()
            ivbox.set_spacing(4)
            vbox.pack_start(ivbox, True, False)
            l = Gtk.Label(label=l)
            ivbox.pack_start(l, True, True, 0)
            ivbox.pack_start(w, True, True, 0)

        self.hbox.pack_start(vbox, False, padding=20)

    def as_tuple(self):
        """Data extraction method."""

        return self._from_channels(), self.message.get_text().strip()


class EditMessageDialog(MessageDialog, EditDialogMixin):

    """Adds delete and restore buttons to a message creation dialog."""

    icon = Gtk.STOCK_EDIT

    def __init__(self, title, orig_data):
        MessageDialog.__init__(self, title)
        EditDialogMixin.__init__(self, orig_data)

    def from_tuple(self, orig_data):
        """The data restore method."""

        self.channels.set_text(orig_data[0])
        self.message.set_text(orig_data[1])


class AnnounceMessageDialog(MessageDialog):

    """Adds delay functionality to the message dialog."""

    # TC: Dialog window title text.
    title = _("IRC track announce")

    def __init__(self):
        MessageDialog.__init__(self)

        self.delay = Gtk.SpinButton(message_delay_adj)
        # TC: Spinbutton label for a delay value.
        self._pack(((_("Delay"), self.delay), ))
        # TC: tooltip on a spinbutton widget.
        set_tip(
            self.delay,
            _("The delay time of this message.\n\nTypically "
              "listener clients will buffer approximately ten "
              "seconds of audio data which means they are listening "
              "the same amount of time behind the actual stream "
              "therefore without a delay IRC messages will appear to "
              "the listener many seconds ahead of the audio.\n\n"
              "This setting will help synchronise the "
              "track change with the message."))

    def as_tuple(self):
        """Data extraction method."""

        return (self.delay.get_value(), ) + MessageDialog.as_tuple(self)


class EditAnnounceMessageDialog(AnnounceMessageDialog, EditDialogMixin):
    icon = Gtk.STOCK_EDIT

    def __init__(self, orig_data):
        AnnounceMessageDialog.__init__(self)
        EditDialogMixin.__init__(self, orig_data)

    def from_tuple(self, orig_data):
        return (self.delay.set_value(orig_data[0]),
                self.channels.set_text(orig_data[1]),
                self.message.set_text(orig_data[2]))


class TimerMessageDialog(MessageDialog):
    # TC: Dialog window title text.
    title = _("IRC timed message")

    def __init__(self):
        MessageDialog.__init__(self)

        self.offset = Gtk.SpinButton(message_offset_adj)
        self.interval = Gtk.SpinButton(message_interval_adj)
        self._pack((
            # TC: Spinbutton time offset value label.
            (_("Offset"), self.offset),
            # TC: Spinbutton timed interval duration value label.
            (_("Interval"), self.interval)))

        # TC: spinbutton tooltip
        set_tip(
            self.offset, (
                _("The time offset within the below specified "
                  "interval at which the message will be issued.")))
        # TC: spinbutton tooltip
        set_tip(
            self.interval, (
                _("The interval in seconds of the timed message.")))

    def as_tuple(self):
        return (
            self.offset.get_value(),
            self.interval.get_value()
        ) + MessageDialog.as_tuple(self)


class EditTimerMessageDialog(TimerMessageDialog, EditDialogMixin):
    icon = Gtk.STOCK_EDIT

    def __init__(self, orig_data):
        TimerMessageDialog.__init__(self)
        EditDialogMixin.__init__(self, orig_data)

    def from_tuple(self, orig_data):
        return (
            self.offset.set_value(orig_data[0]),
            self.interval.set_value(orig_data[1]),
            self.channels.set_text(orig_data[2]),
            self.message.set_text(orig_data[3]))


def glue(f):
    """IRCPane function decorator for new/edit button callbacks.

    Provides item infrormation and wires up the edit dialogs.
    """

    @wraps(f)
    def inner(self, widget):
        model, _iter = self._treeview.get_selection().get_selected()

        if _iter is not None:
            def dialog(d, cb, *args, **kwds):
                cancel = Gtk.Button(Gtk.STOCK_CANCEL)
                d.ok = Gtk.Button(Gtk.STOCK_OK)
                bb = d.get_action_area()
                for each in (cancel, d.ok):
                    each.set_use_stock(True)
                    each.connect_after("clicked", lambda w: d.destroy())
                    bb.add(each)

                d.set_modal(True)
                d.set_transient_for(self.get_toplevel())
                d.ok.connect(
                    "clicked",
                    lambda w: cb(d, model, _iter, *args, **kwds))

                if hasattr(d, "delete"):
                    @d.delete_confirmation
                    def delete(w):
                        iter_parent = model.iter_parent(_iter)
                        self._treeview.get_selection().select_iter(iter_parent)
                        model.remove(_iter)
                        d.destroy()

                    d.delete.connect("clicked", delete)

                d.show_all()

            return f(self, model.get_value(_iter, 0), model, _iter, dialog)
        else:
            return None
    return inner


def highlight(f):
    """IRCPane function decorator to highlight newly added item."""

    @wraps(f)
    def inner(self, mode, model, iter, *args, **kwds):
        new_iter = f(self, mode, model, iter, *args, **kwds)

        path = model.get_path(new_iter)
        self._treeview.expand_to_path(path)
        self._treeview.expand_row(path, True)
        self._treeview.get_selection().select_path(path)

        return new_iter
    return inner


class IRCTreeView(Gtk.TreeView):

    """A Gtk.TreeView that has a tooltip which handles IRC text formatting."""

    def __init__(self, model=None):
        super(IRCTreeView, self).__init__()
        self.set_model(model)
        self.set_headers_visible(False)
        self.set_enable_tree_lines(True)
        self.connect("query-tooltip", self._on_query_tooltip)
        self.set_has_tooltip(True)
        self.tooltip_coords = (0, 0)

    def _on_query_tooltip(self, tv, x, y, kb_mode, tooltip):
        """Display an IRCView tooltip for appropriate data elements."""

        if (x, y) != self.tooltip_coords:
            self.tooltip_coords = (x, y)
        elif None not in (x, y):
            path = tv.get_path_at_pos(
                *tv.convert_widget_to_bin_window_coords(x, y))
            if path is not None:
                model = tv.get_model()
                iter = model.get_iter(path[0])
                mode = model.get_value(iter, 0)
                if mode in (3, 5, 7, 9):
                    message = model[model.get_path(iter)].message
                    irc_view = IRCView()
                    irc_view.set_text(message)
                    tooltip.set_custom(irc_view)
                    return True


class IRCRowReference(NamedTreeRowReference):

    """A Gtk.TreeRowReference but with named attributes.

    The naming scheme depends on the data type of each row.
    """

    _lookup = {
        1: {
            "manual": 2,
            "port": 3,
            "unused": 4,
            "network": 5,
            "hostname": 6,
            "username": 7,
            "password": 8,
            "nick1": 9,
            "nick2": 10,
            "nick3": 11,
            "realname": 12,
            "nickserv": 13,
            "nick": 14},
        3: {
            "delay": 4,
            "channels": 5,
            "message": 6},
        5: {
            "offset": 3,
            "interval": 4,
            "channels": 5,
            "message": 6,
            "issue": 14},
        7: {
            "channels": 5,
            "message": 6},
        9: {
            "channels": 5,
            "message": 6},
        11: {"channels": 5}
    }

    def get_index_for_name(self, tree_row_ref, name):
        """An abstract method of the base class that performs the lookup."""

        if name == "type":
            return 0
        elif name == "active":
            return 1
        else:
            data_type = tree_row_ref[0]
            return self._lookup[data_type][name]


class IRCTreeStore(Gtk.TreeStore):

    """The data storage object."""

    @property
    def data_format(self):
        return (int, ) * 5 + (str, ) * 10

    def __init__(self):
        super(IRCTreeStore, self).__init__()
        self.set_column_types(self.data_format)
        self._row_changed_blocked = False
        self.connect_after("row-changed", self._on_row_changed)

    def path_is_active(self, path):
        """True when this and all parent elements are active."""

        while self[path].active:
            path = path[:-1]
            if not path:
                return True

        return False

    def row_changed_block(self):
        self._row_changed_blocked = True

    def row_changed_unblock(self):
        self._row_changed_blocked = False

    def _on_row_changed(self, model, path, iter):
        """This is the very first handler that will be called."""

        if self._row_changed_blocked:
            self.stop_emission("row-changed")

    def __getitem__(self, path):
        """Properly wrap the TreeRowReference."""

        return IRCRowReference(Gtk.TreeStore.__getitem__(self, path))


class IRCPane(Gtk.VBox):

    """The main user interface."""

    def __init__(self):
        super(IRCPane, self).__init__()
        self.set_border_width(8)
        self.set_spacing(3)
        self._treestore = IRCTreeStore()
        self._treestore.insert(None, 0, (0, 1, 0, 0, 0) + ("", ) * 10)
        self._treeview = IRCTreeView(self._treestore)

        col = Gtk.TreeViewColumn()
        toggle = Gtk.CellRendererToggle()
        toggle.props.sensitive = False
        col.pack_start(toggle, False)
        col.add_attribute(toggle, "active", 1)

        crt = Gtk.CellRendererText()
        crt.props.ellipsize = Pango.EllipsizeMode.END
        col.pack_start(crt, True)
        col.set_cell_data_func(crt, self._cell_data_func)

        self._treeview.append_column(col)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self._treeview)

        bb = Gtk.HButtonBox()
        bb.set_spacing(6)
        bb.set_layout(Gtk.ButtonBoxStyle.END)
        edit = Gtk.Button(Gtk.STOCK_EDIT)
        new = Gtk.Button(Gtk.STOCK_NEW)
        for b, c in zip((edit, new), ("edit", "new")):
            b.set_use_stock(True)
            b.connect("clicked", getattr(self, "_on_" + c))
            bb.add(b)

        toggle_button = Gtk.Button("_Toggle")
        toggle_button.connect("clicked", self._on_toggle)
        bb.add(toggle_button)
        bb.set_child_secondary(toggle_button, True)

        selection = self._treeview.get_selection()
        selection.connect("changed", self._on_selection_changed, edit, new)
        selection.select_path(0)

        if HAVE_IRC:
            self.pack_start(sw, True, True, 0)
            self.pack_start(bb, False)
            self.connections_controller = ConnectionsController(
                self._treestore)
        else:
            self.set_sensitive(False)
            label = Gtk.Label(
                label=_("This feature requires the "
                        "installation of python-irc."))
            self.add(label)
            self.connections_controller = ConnectionsController(None)

        self.show_all()

    def _m_signature(self):
        """The client data storage signature.

        Used to crosscheck with that of the saved data to test for usability.
        """

        return [x.__name__ for x in self._treestore.data_format]

    def marshall(self):
        """Convert all our data into a string."""

        if HAVE_IRC:
            store = [self._m_signature()]
            self._treestore.foreach(self._m_read, store)
            return json.dumps(store)
        else:
            return ""

    def _m_read(self, model, path, iter, store):
        row = IRCRowReference(list(model[path]))
        if row.type == 1 and row.active and row.manual:
            row.active = 0

        store.append((path, list(row)))

    def unmarshall(self, data):
        """Set the TreeStore with data from a string."""

        if HAVE_IRC:
            try:
                store = json.loads(data)
            except ValueError:
                return

            if store.pop(0) != self._m_signature():
                print("IRC server data format mismatch.")
                return

            selection = self._treeview.get_selection()
            selection.handler_block_by_func(self._on_selection_changed)
            self._treestore.clear()
            for path, row in store:
                pos = path.pop()
                pi = self._treestore.get_iter(tuple(path)) if path else None
                self._treestore.insert(pi, pos, row)
            self._treeview.expand_all()
            selection.handler_unblock_by_func(self._on_selection_changed)
            selection.select_path(0)

    def _on_selection_changed(self, selection, edit, new):
        model, iter = selection.get_selected()
        if iter is not None:
            mode = model.get_value(iter, 0)

            edit.set_sensitive(mode % 2)
            new.set_sensitive(not mode % 2)
        else:
            edit.set_sensitive(False)
            new.set_sensitive(False)

    def _on_toggle(self, widget):
        model, iter = self._treeview.get_selection().get_selected()
        model.set_value(iter, 1, not model.get_value(iter, 1))

    def _cell_data_func(self, column, cell, model, iter):
        """Converts tree data into something viewable.

        There is only one line to display on so the actual text is not
        given too much priority. For that there is the tooltip IRCView.
        """

        row = model[model.get_path(iter)]
        text = ""

        if row.type % 2:
            if row.type == 1:
                if row.nick:
                    text = row.nick + "@"
                text += "%s:%d" % (row.hostname, row.port)
                if row.network:
                    text += "(%s)" % row.network

                opt = []
                if row.password:
                    # TC: Indicator text: We used a password.
                    opt.append(_("PASSWORD"))
                if row.nickserv:
                    # TC: Indicator text: We interact with NickServ.
                    opt.append(_("NICKSERV"))
                if row.manual:
                    # TC: Indicator text: Server connection started manually.
                    opt.append(_("MANUAL"))
                if opt:
                    text += " " + ", ".join(opt)
            else:
                channels = row.channels

                if row.type < 11:
                    message = row.message

                    if row.type == 3:
                        text = "+%d;%s; %s" % (row.delay, channels, message)
                    elif row.type == 5:
                        text = "%d/%d;%s; %s" % (
                            row.offset,
                            row.interval,
                            channels,
                            message)
                    elif row.type in (7, 9):
                        text = channels + "; " + message
                elif row.type == 11:
                    text = channels
        else:
            text = (("Server", ) + MESSAGE_CATEGORIES)[row.type / 2]

        cell.props.text = text

    # TC: Dialog title text.
    _dsu = _("IRC stream up message")
    # TC: Dialog title text.
    _dsd = _("IRC stream down message")
    # TC: Dialog title text.
    _dso = _("IRC station operations")

    @glue
    def _on_new(self, mode, model, iter, dialog):
        if mode == 0:
            dialog(ServerDialog(), self._add_server)
        elif mode == 2:
            dialog(AnnounceMessageDialog(), self._add_announce)
        elif mode == 4:
            dialog(TimerMessageDialog(), self._add_timer)
        elif mode in (6, 8):
            title = self._dsu if mode == 6 else self._dsd
            dialog(MessageDialog(title), self._add_message, mode)
        elif mode == 10:
            dialog(ChannelsDialog(self._dso), self._add_channels, mode)
        else:
            self._unhandled_mode(mode)

    @glue
    def _on_edit(self, mode, model, iter, dialog):
        row = tuple(model[model.get_path(iter)])

        if mode == 1:
            dialog(EditServerDialog(row[2:14]), self._standard_edit, 2)
        elif mode == 3:
            dialog(EditAnnounceMessageDialog(row[4:7]), self._standard_edit, 4)
        elif mode == 5:
            dialog(EditTimerMessageDialog(row[3:7]), self._standard_edit, 3)
        elif mode in (7, 9):
            title = self._dsu if mode == 7 else self._dsd
            dialog(EditMessageDialog(title, row[5:7]), self._standard_edit, 5)
        elif mode == 11:
            dialog(
                EditChannelsDialog(
                    self._dso,
                    row[5:6]),
                self._standard_edit,
                5)
        else:
            self._unhandled_mode(mode)

    @staticmethod
    def _unhandled_mode(mode):
        print("unhandled message category with numerical code,", mode)

    def _standard_edit(self, d, model, iter, start):
        model.row_changed_block()
        for i, each in enumerate(d.as_tuple(), start=start):
            model.set_value(iter, i, each)
        model.row_changed_unblock()
        model.row_changed(model.get_path(iter), iter)

    @highlight
    def _add_server(self, d, model, parent_iter):
        # Check whether row initially needs to be switched off.
        row = IRCRowReference(list((1, 1) + d.as_tuple() + ("", )))
        if row.manual:
            row.active = 0

        iter = model.insert(parent_iter, 0, row)

        # Add the subelements.
        for i, x in enumerate(range(2, 2 + len(MESSAGE_CATEGORIES) * 2, 2)):
            model.insert(iter, i, (x, 1, 0, 0, 0) + ("", ) * 10)

        return iter

    @highlight
    def _add_announce(self, d, model, parent_iter):
        return model.insert(
            parent_iter,
            0,
            (3, 1, 0, 0) + d.as_tuple() + ("", ) * 8)

    @highlight
    def _add_timer(self, d, model, parent_iter):
        return model.insert(
            parent_iter,
            0,
            (5, 1, 0) + d.as_tuple() + ("", ) * 8)

    @highlight
    def _add_message(self, d, model, parent_iter, mode):
        return model.insert(
            parent_iter,
            0,
            (mode + 1, 1, 0, 0, 0) + d.as_tuple() + ("", ) * 8)

    @highlight
    def _add_channels(self, d, model, parent_iter, mode):
        return model.insert(parent_iter, 0, (mode + 1, 1, 0, 0, 0)
                            + d.as_tuple() + ("", ) * 9)


class ConnectionsController(list):

    """Layer between the user interface and the ServerConnection classes.

    As a list it contains the active server connections.
    """

    def __init__(self, model):
        self.model = model
        self._ignore_count = 0
        if model is not None:
            model.connect("row-inserted", self._on_row_inserted)
            model.connect("row-deleted", self._on_row_deleted)
            model.connect_after("row-changed", self._on_row_changed)

        list.__init__(self)
        self._stream_active = False

    def cleanup(self):
        for each in self:
            each.cleanup()

    def set_stream_active(self, stream_active):
        self._stream_active = stream_active

        for each in self:
            each.set_stream_active(stream_active)

    def new_metadata(self, new_meta):
        for each in self:
            each.new_metadata(new_meta)

    def _on_row_inserted(self, model, path, iter):
        if model.get_value(iter, 0) == 1:
            self.append(IRCConnection(model, path, self._stream_active))

    def _on_row_deleted(self, model, path):
        if len(path) == 2:
            for i, irc_conn in enumerate(self):
                if not irc_conn.valid():
                    self[i].cleanup()
                    del self[i]
                    break

    def _on_row_changed(self, model, path, iter):
        i = model.iter_children(iter)
        while i is not None:
            model.row_changed(model.get_path(i), i)
            i = model.iter_next(i)


class IRCConnection(Gtk.TreeRowReference, threading.Thread):

    """Self explanatory really."""

    def __init__(self, model, path, stream_active):
        super(IRCConnection, self).__init__(model, path)
        threading.Thread.__init__(self)
        self._hooks = []
        self._queue = []
        self._played = []
        self._message_handlers = []
        self._keepalive = True
        self._have_welcome = False
        self._stream_active = stream_active
        try:
            self.reactor = client.Reactor()
        except AttributeError:
            self.reactor = client.IRC()  # Old API compatibility

        self.server = self.reactor.server()
        self.start()
        self._hooks.append((model, model.connect(
            "row-inserted",
            self._on_row_inserted)))
        self._hooks.append((model, model.connect_after(
            "row-changed",
            self._on_ui_row_changed)))
        self._on_ui_row_changed(model, path, model.get_iter(path))

    def set_stream_active(self, stream_active):
        self._stream_active = stream_active
        for each in self._message_handlers:
            each.set_stream_active(stream_active)

    def new_metadata(self, new_meta):
        if self._stream_active:
            self._played.insert(0, (new_meta["songname"], time.time()))
            del self._played[10:]

        for each in self._message_handlers:
            each.new_metadata(new_meta)

    def _on_row_inserted(self, model, path, iter):
        if path[:-1] == self.get_path():
            type = model[path].type
            mh = globals()["MessageHandlerForType_" + str(type + 1)](
                model,
                path,
                self._stream_active)
            mh.connect("channels-changed", self._on_channels_changed)
            mh.connect("privmsg-ready", self._on_privmsg_ready)
            self._message_handlers.append(mh)

    def _on_channels_changed(self, message_handler, channel_set):
        if self._have_welcome:
            rest = frozenset.union(frozenset(), *(
                x.props.channels
                for x in self._message_handlers if x is not message_handler))

            joins = channel_set.difference(rest and
                                           message_handler.props.channels)

            parts = message_handler.props.channels.difference(
                channel_set).difference(rest)

            def deferred():
                for each in joins:
                    if each[0] in "#&":
                        each = each.split(":")
                        try:
                            channel, key = each
                        except ValueError:
                            channel = each[0]
                            key = ""
                        self.server.join(channel, key)

                for each in parts:
                    if each[0] in "#&":
                        self.server.part(each)

            self._queue.append(deferred)

    def _channels_invalidate(self):
        for each in self._message_handlers:
            each.channels_invalidate()

    def _on_privmsg_ready(self, handler, targets, message, delay):
        if self._have_welcome:
            chan_targets = [x.split(":")[0] for x in targets if x[0] in "#&"]
            user_targets = [x for x in targets if x[0] not in "#&"]

            def deferred():
                self.server.privmsg_many(chan_targets, message)
                for target in user_targets:
                    self.server.notice(target, message)

            if delay:
                self._queue.append(
                    lambda: self.server.execute_delayed(delay, deferred))
            else:
                self._queue.append(deferred)

    def _on_ui_row_changed(self, model, path, iter):
        if path == self.get_path():
            row = self.get_model()[self.get_path()]
            if model.path_is_active(path):
                ref = Gtk.TreeRowReference(model, path)
                hostname = row.hostname
                port = row.port
                nickname = row.nick1 or "eyedeejaycee"
                password = row.password or None
                username = row.username or None
                ircname = row.realname or None
                opts = {}

                def deferred():
                    self._alternates = [
                        row.nick2, row.nick3, nickname + "_",
                        row.nick2 + "_", row.nick3 + "_", nickname + "__",
                        row.nick2 + "__", row.nick3 + "__"]

                    connect = partial(
                        self.server.connect,
                        hostname,
                        port,
                        nickname,
                        password,
                        username,
                        ircname)

                    def try_connect(*delays):
                        model = ref.get_model()
                        path = ref.get_path()
                        if not ref.valid() or not model.path_is_active(path):
                            print("IRC connection attempt cancelled")
                            return

                        print("Attempting to connect IRC %s:%d" % (
                            hostname, port))
                        try:
                            connect()
                        except client.ServerConnectionError as e:
                            print(e)
                            try:
                                delay = delays[0]
                            except IndexError:
                                print("No more connection attempts")
                                self._ui_set_nick("")
                            else:
                                print("%d more tries" % len(delays))
                                self.server.execute_delayed(delay, try_connect,
                                                            delays[1:])
                        else:
                            self._ui_set_nick(nickname)
                            print("New IRC connection: %s@%s:%d" % (
                                nickname, hostname, port))

                    try_connect(1, 2, 3)
            else:
                def deferred():
                    try:
                        self.server.disconnect()
                    except client.ServerConnectionError as e:
                        print(str(e), file=sys.stderr)
                    self._ui_set_nick("")

            self._queue.append(deferred)

    def run(self):
        for event in events.all:
            try:
                target = getattr(self, "_on_" + event)
            except AttributeError:
                target = self._generic_handler
            self.server.add_global_handler(event, target)

        while self._keepalive:
            while len(self._queue):
                self._queue.pop(0)()

            self.reactor.process_once(0.2)

        self.reactor.process_once()

    def cleanup(self):
        for each in self._message_handlers:
            each.cleanup()
        for obj, handler_id in self._hooks:
            obj.disconnect(handler_id)

        if self.server.is_connected():
            def deferred():
                self.server.add_global_handler("disconnect", self.end_thread)
                try:
                    self.server.disconnect()
                except client.ServerConnectionError as e:
                    print(str(e), file=sys.stderr)
                self._ui_set_nick("")

            self._queue.append(deferred)
        else:
            self._keepalive = False

        self.join(1.0)

    def end_thread(self, server, event):
        self._keepalive = False

    @threadslock
    def _ui_set_nick(self, nickname):
        if self.valid():
            model = self.get_model()
            model.row_changed_block()
            model[self.get_path()].nick = nickname
            model.row_changed_unblock()

    def _try_alternate_nick(self):
        try:
            nextnick = self._alternates.pop(0)
        except IndexError:
            # Ran out of nick choices.
            self.server.disconnect()
        else:
            self._ui_set_nick(nextnick)
            self.server.nick(nextnick)

    @threadslock
    def _on_welcome(self, server, event):
        print("Got IRC welcome", event.source)
        self._have_welcome = True
        self._channels_invalidate()
        model = self.get_model()
        path = self.get_path()
        iter = model.iter_children(model.get_iter(path))
        while iter is not None:
            model.row_changed(model.get_path(iter), iter)
            iter = model.iter_next(iter)
        row = model[path]
        model.row_changed_block()
        row.nick = event.target
        model.row_changed_unblock()

        target = row.nick1
        nspw = row.nickserv
        if event.target != target and nspw:
            self._nick_recover(server, target, nspw)

    def _nick_recover(self, server, target, nspw):
        print("Will issue recover and release commands to NickServ")
        for i, (func, args) in enumerate((
                (server.privmsg, (
                    "NickServ", "RECOVER %s %s" % (target, nspw))),
                (server.privmsg, (
                    "NickServ", "RELEASE %s %s" % (target, nspw))),
                (server.nick, (target,))), start=1):

            server.execute_delayed(i, func, args)

    def _on_privnotice(self, server, event):
        source = event.source
        if source is not None:
            source = source.split("@")[0]

            if source != "Global!services":
                print("-%s- %s" % (source, event.arguments[0]))

            if source == "NickServ!services":
                with gdklock():
                    nspw = self.get_model()[self.get_path()].nickserv

                if "NickServ IDENTIFY" in event.arguments[0] and nspw:
                    server.privmsg("NickServ", "IDENTIFY %s" % nspw)
                    print("Issued IDENTIFY command to NickServ")
                    self._ui_set_nick(event.target)
                elif "Guest" in event.arguments[0]:
                    newnick = event.arguments[0].split()[-1].strip(ASCII_C0)
                    self._ui_set_nick(newnick)
                    if nspw:
                        self._nick_recover(server, event.target, nspw)
                else:
                    self._ui_set_nick(event.target)
        else:
            self._generic_handler(server, event)

    def _on_disconnect(self, server, event):
        self._have_welcome = False
        self._ui_set_nick("")
        print(event.source, "disconnected")

    def _on_nicknameinuse(self, server, event):
        self._try_alternate_nick()

    def _on_nickcollision(self, server, event):
        self._try_alternate_nick()

    def _on_nonicknamegiven(self, server, event):
        self._try_alternate_nick()

    def _on_erroneousenickname(self, server, event):
        self._try_alternate_nick()

    def _on_join(self, server, event):
        print("Channel joined", event.target)

    def _on_ctcp(self, server, event):
        source = event.source.split("!")[0]
        args = event.arguments
        reply = partial(server.ctcp_reply, source)

        if args == ["CLIENTINFO"]:
            reply("CLIENTINFO VERSION TIME SOURCE PING ACTION CLIENTINFO "
                  "PLAYED STREAMSTATUS KILLSTREAM")

        elif args == ["VERSION"]:
            reply("VERSION %s %s (python-irc)" % (
                FGlobs.package_name, FGlobs.package_version))
        elif args == ["TIME"]:
            reply("TIME " + time.ctime())

        elif args == ["SOURCE"]:
            reply("SOURCE http://www.sourceforge.net/projects/idjc")

        elif args[0] == "PING":
            reply(" ".join(args))

        elif args == ["PLAYED"]:
            t = time.time()
            with gdklock():
                show = [x for x in self._played if t - x[1] < 5400.0]

            for i, each in enumerate(show, start=1):
                age = int((t - each[1]) // 60)
                if age == 1:
                    message = "PLAYED \x0304%s\x0f, \x0306%d minute ago\x0f."
                else:
                    message = "PLAYED \x0304%s\x0f, \x0306%d minutes ago\x0f."
                server.execute_delayed(i, reply, (message %
                                                  (each[0], age),))

            if not show:
                reply("PLAYED Nothing recent to report.")
            else:
                server.execute_delayed(i + 1, reply, ("PLAYED End of list.",))

        elif args == ["STREAMSTATUS"]:
            reply("STREAMSTATUS The stream is %s." % (
                "up" if self._stream_active else "down"))

        elif args == ["KILLSTREAM"]:
            reply("KILLSTREAM This feature was added as a joke.")

        elif args == ["ACTION"]:
            pass

        else:
            pass
            # print "CTCP from", source, args

    def _on_motd(self, server, event):
        pass

    def _generic_handler(self, server, event):
        return
        print("Type:", event.eventtype())
        print("Source:", event.source())
        print("Target:", event.target())
        print("Args:", event.arguments())


class MessageHandler(GObject.GObject):
    __gsignals__ = {
        'channels-changed': (
            GObject.SignalFlags.RUN_LAST | GObject.SignalFlags.ACTION,
            None, (GObject.TYPE_PYOBJECT, )
        ),

        'privmsg-ready': (
            GObject.SignalFlags.RUN_LAST | GObject.SignalFlags.ACTION,
            None, (GObject.TYPE_PYOBJECT,
                   GObject.TYPE_STRING, GObject.TYPE_INT)
        )

    }

    __gproperties__ = {
        'channels': (
            GObject.TYPE_PYOBJECT, 'channels', 'ircchannels',
            GObject.PARAM_READABLE)
    }

    @property
    def stream_active(self):
        return self._stream_active

    subst_keys = (
        "artist",
        "title",
        "album",
        "songname",
        "djname",
        "description",
        "url",
        "source")

    subst_tokens = ("%r", "%t", "%l", "%s", "%n", "%d", "%u", "%U")

    subst = dict.fromkeys(subst_keys, "<No data>")

    def __init__(self, model, path, stream_active):
        super(MessageHandler, self).__init__()
        self.tree_row_ref = Gtk.TreeRowReference(model, path)

        self._channels = frozenset()
        self._stream_active = stream_active
        model.connect("row-inserted", self.channels_evaluate)
        model.connect("row-deleted", self.channels_evaluate)
        model.connect_after("row-changed", self.channels_evaluate)

    def set_stream_active(self, stream_active):
        if self._stream_active != stream_active:
            self._stream_active = stream_active
            if stream_active:
                self.on_stream_active()
            else:
                self.on_stream_inactive()

    def on_stream_active(self):
        pass

    def on_stream_inactive(self):
        pass

    def cleanup(self):
        pass

    def on_new_metadata(self):
        pass

    def new_metadata(self, new_meta):
        assert not frozenset(new_meta).difference(frozenset(self.subst_keys))

        self.subst.update(new_meta)
        self.on_new_metadata()

    def channels_evaluate(self, model, path, iter=None):
        pp = self.tree_row_ref.get_path()
        if path[:-1] == pp:
            nc = set()

            iter = model.iter_children(model.get_iter(pp))
            while iter is not None:
                rowpath = model.get_path(iter)
                if model.path_is_active(rowpath):
                    row = model[rowpath]
                    for each in row.channels.split(","):
                        if each:
                            nc.add(each)
                iter = model.iter_next(iter)

            nc = frozenset(nc)
            if nc != self._channels:
                self.channels_changed(nc)

    def channels_invalidate(self):
        self._channels = frozenset()

    def channels_changed(self, new_channels):
        self.emit("channels-changed", new_channels)

    def do_channels_changed(self, new_channels):
        """Called after the handlers connected on 'channels-changed'.

        Joins and parts may be computed against self.props.channels.
        """

        self._channels = frozenset(new_channels)

    def do_get_property(self, prop):
        if prop.name == 'channels':
            return self._channels
        else:
            raise AttributeError("unknown property '%s'" % prop.name)

    def issue_messages(self, delay_calc=lambda row: 0, forced_message=None):
        model = self.tree_row_ref.get_model()
        iter = model.get_iter(self.tree_row_ref.get_path())
        iter = model.iter_children(iter)
        while iter is not None:
            path = model.get_path(iter)
            if model.path_is_active(path):
                row = model[path]
                delay_s = delay_calc(row)
                if delay_s is not None:
                    targets = [x.split("!")[0]
                               for x in row.channels.split(",")]
                    table = [("%%", "%")] + list(zip(self.subst_tokens, (
                        self.subst[x] for x in self.subst_keys)))
                    if forced_message is not None:
                        message = string_multireplace(forced_message, table)
                    else:
                        message = string_multireplace(row.message, table)
                    self.emit("privmsg-ready", targets, message, delay_s)

            iter = model.iter_next(iter)


class MessageHandlerForType_3(MessageHandler):

    def on_new_metadata(self):
        if self.stream_active:
            self.issue_messages(lambda row: row.delay)


class MessageHandlerForType_5(MessageHandler):

    def __init__(self, *args, **kwargs):
        self._timeout_id = None
        MessageHandler.__init__(self, *args, **kwargs)
        if self.stream_active:
            self.on_stream_active()

    def on_stream_active(self):
        self._timeout_id = timeout_add(500, self._timeout)

    def on_stream_inactive(self):
        if self._timeout_id is not None:
            source_remove(self._timeout_id)
            self._timeout_id = None

    @threadslock
    def _timeout(self):
        self.issue_messages(partial(self._delay_calc,
                                    the_time=int(time.time())))
        return True

    def _delay_calc(self, row, the_time):
        """Returns either a delay of 0 or suppression value None."""

        issue = (the_time - row.offset) // row.interval
        if issue > int(row.issue or 0):
            row.issue = str(issue)
            return 0

    def cleanup(self):
        if self._timeout_id is not None:
            source_remove(self._timeout_id)


class MessageHandlerForType_7(MessageHandler):

    def on_stream_active(self):
        self.issue_messages()


class MessageHandlerForType_9(MessageHandler):

    def on_stream_inactive(self):
        self.issue_messages()


class MessageHandlerForType_11(MessageHandler):

    def on_stream_active(self):
        self.issue_messages(forced_message="!handover acquired %U")

    def on_stream_inactive(self):
        self.issue_messages(forced_message="!handover dropped %U")
