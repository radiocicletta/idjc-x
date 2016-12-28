"""Generally useful gtk based widgets."""

#   Copyright (C) 2011 Stephen Fairchild (s-fairchild@users.sourceforge.net)
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
import json
import gettext
from abc import ABCMeta, abstractmethod
from functools import wraps
from contextlib import contextmanager

import gobject
import gtk
import pango
import glib

from idjc import FGlobs, PGlobs


t = gettext.translation(FGlobs.package_name, FGlobs.localedir, fallback=True)
_ = t.gettext


# Fix PyGTK spin button binding.
def SB():
    GTK_SB = gtk.SpinButton
    
    class SpinButton(GTK_SB):
        def __init__(self, adjustment=None, climb_rate=0.0, digits=0):
            GTK_SB.__init__(self, adjustment, climb_rate, digits)
        
    return SpinButton
        
gtk.SpinButton = SB()
del SB


class NotebookSR(gtk.Notebook):
    """Add methods so the save/restore scheme does not have to be extended."""

    def get_active(self):
        return self.get_current_page()
        
    def set_active(self, page):
        self.set_current_page(page)


class LEDDict(dict):
    """Dictionary of pixbufs of LEDs."""
        
    def __init__(self, size=10):
        names = "clear", "red", "green", "yellow"
        filenames = ("led_unlit_clear_border_64x64.png",
                         "led_lit_red_black_border_64x64.png",
                         "led_lit_green_black_border_64x64.png",
                         "led_lit_amber_black_border_64x64.png")
        for name, filename in zip(names, filenames):
            self[name] = gtk.gdk.pixbuf_new_from_file_at_size(
                FGlobs.pkgdatadir / filename, size, size)


class CellRendererLED(gtk.CellRendererPixbuf):
    """A cell renderer that displays LEDs."""
        
    __gproperties__ = {
            "active" : (gobject.TYPE_INT, "active", "active",
                            0, 1, 0, gobject.PARAM_WRITABLE),
            "color" :  (gobject.TYPE_STRING, "color", "color",
                            "clear", gobject.PARAM_WRITABLE)
    }

    def __init__(self, size=10, actives=("clear", "green")):
        gtk.CellRendererPixbuf.__init__(self)
        self._led = LEDDict(size)
        self._index = [self._led[key] for key in actives] 

    def do_set_property(self, prop, value):
        if prop.name == "active":
            item = self._index[value]
        elif prop.name == "color":
            item = self._led[value]
        else:
            raise AttributeError("unknown property %s" % prop.name)
            
        gtk.CellRendererPixbuf.set_property(self, "pixbuf", item)


class CellRendererTime(gtk.CellRendererText):
    """Displays time in days, hours, minutes."""
    
    
    __gproperties__ = {
            "time" : (gobject.TYPE_INT, "time", "time",
                         0, 1000000000, 0, gobject.PARAM_WRITABLE)
    }
    
    
    def do_set_property(self, prop, value):
        if prop.name == "time":
            m, s = divmod(value, 60)
            h, m = divmod(m, 60)
            d, h = divmod(h, 24)
            if d:
                text = "%dd.%02d:%02d" % (d, h, m)
            else:
                text = "%02d:%02d:%02d" % (h, m, s)
        else:
            raise AttributeError("unknown property %s" % prop.name)
            
        gtk.CellRendererText.set_property(self, "text", text)


class StandardDialog(gtk.Dialog):
    def __init__(self, title, message, stock_item, label_width, modal, markup):
        gtk.Dialog.__init__(self)
        self.set_border_width(6)
        self.get_child().set_spacing(12)
        self.set_modal(modal)
        self.set_destroy_with_parent(True)
        self.set_title(title)
        
        hbox = gtk.HBox()
        hbox.set_spacing(12)
        hbox.set_border_width(6)
        image = gtk.image_new_from_stock(stock_item,
                                                        gtk.ICON_SIZE_DIALOG)
        image.set_alignment(0.0, 0.0)
        hbox.pack_start(image, False)
        vbox = gtk.VBox()
        hbox.pack_start(vbox)
        for each in message.split("\n"):
            label = gtk.Label(each)
            label.set_use_markup(markup)
            label.set_alignment(0.0, 0.0)
            label.set_size_request(label_width, -1)
            label.set_line_wrap(True)
            vbox.pack_start(label)
        ca = self.get_content_area()
        ca.add(hbox)
        aa = self.get_action_area()
        aa.set_spacing(6)


class ConfirmationDialog(StandardDialog):
    """This needs to be pulled out since it's generic."""
    
    def __init__(self, title, message, label_width=300, modal=True,
            markup=False, action=gtk.STOCK_DELETE, inaction=gtk.STOCK_CANCEL):
        StandardDialog.__init__(self, title, message,
                        gtk.STOCK_DIALOG_WARNING, label_width, modal, markup)
        aa = self.get_action_area()
        cancel = gtk.Button(stock=inaction)
        cancel.connect("clicked", lambda w: self.destroy())
        aa.pack_start(cancel)
        self.ok = gtk.Button(stock=action)
        self.ok.connect_after("clicked", lambda w: self.destroy())
        aa.pack_start(self.ok)


class ErrorMessageDialog(StandardDialog):
    """This needs to be pulled out since it's generic."""
    
    def __init__(self, title, message, label_width=300, modal=True,
                                                                markup=False):
        StandardDialog.__init__(self, title, message,
                            gtk.STOCK_DIALOG_ERROR, label_width, modal, markup)
        b = gtk.Button(stock=gtk.STOCK_CLOSE)
        b.connect("clicked", lambda w: self.destroy())
        self.get_action_area().add(b)


def threadslock(inner):
    """Function decorator to safely apply gtk/gdk thread lock to callbacks.
    
    Needed to lock non gtk/gdk callbacks originating in the wider glib main
    loop whenever they may call gtk or gdk code, read properties etc.
    
    Useful for callbacks that mainly manipulate gtk.
    """
    
    @wraps(inner)
    def wrapper(*args, **kwargs):
        gtk.gdk.threads_enter()
        try:
            if gtk.main_level():
                return inner(*args, **kwargs)
            else:
                # Cancel timeouts and idle functions.
                print("callback cancelled")
                return False
        finally:
            gtk.gdk.threads_leave()
    return wrapper


@contextmanager
def gdklock():
    """Like threadslock but for 'with' code blocks that manipulate gtk."""
    
    gtk.gdk.threads_enter()
    yield
    gtk.gdk.threads_leave()
    
    
@contextmanager
def gdkunlock():
    """Like gdklock but unlock instead.
    
    Useful for calling threadslock functions when already locked.
    """
    
    gtk.gdk.threads_leave()
    yield
    gtk.gdk.threads_enter()


@contextmanager
def nullcm():
    """Null context.
    
    eg. with (gdklock if lock_f else nullcm)():"""
    
    yield


class DefaultEntry(gtk.Entry):
    def __init__(self, default_text, sensitive_override=False):
        gtk.Entry.__init__(self)
        self.connect("focus-in-event", self.on_focus_in)
        self.connect("focus-out-event", self.on_focus_out)
        self.props.primary_icon_activatable = True
        self.connect("icon-press", self.on_icon_press)
        self.connect("realize", self.on_realize)
        self.default_text = default_text
        self.sensitive_override = sensitive_override

    def on_realize(self, entry):
        layout = self.get_layout().copy()
        layout.set_markup("<span foreground='dark gray'>%s</span>" %
                                                            self.default_text)
        extents = layout.get_pixel_extents()[1]
        drawable = gtk.gdk.Pixmap(self.get_parent_window(), extents[2],
                                                            extents[3])
        gc = gtk.gdk.GC(drawable)
        gc2 = entry.props.style.base_gc[0]
        drawable.draw_rectangle(gc2, True, *extents)
        drawable.draw_layout(gc, 0, 0, layout)
        pixbuf = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, True, 8, extents[2],
                                                            extents[3])
        pixbuf.get_from_drawable(drawable, drawable.get_colormap(), 0, 0,
                                                            *extents)
        self.empty_pixbuf = pixbuf
        if not gtk.Entry.get_text(self):
            self.props.primary_icon_pixbuf = pixbuf

    def on_icon_press(self, entry, icon_pos, event):
        self.grab_focus()
        
    def on_focus_in(self, entry, event):
        self.props.primary_icon_pixbuf = None
        
    def on_focus_out(self, entry, event):
        text = gtk.Entry.get_text(self).strip()
        if not text:
            self.props.primary_icon_pixbuf = self.empty_pixbuf
        
    def get_text(self):
        if (self.flags() & gtk.SENSITIVE) or self.sensitive_override:
            return gtk.Entry.get_text(self).strip() or self.default_text
        else:
            return ""
        
    def set_text(self, newtext):
        newtext = newtext.strip()
        gtk.Entry.set_text(self, newtext)
        if newtext:
            self.props.primary_icon_pixbuf = None
        else:
            try:
                self.props.primary_icon_pixbuf = self.empty_pixbuf
            except AttributeError:
                pass


class HistoryEntry(gtk.ComboBoxEntry):
    """Combobox which performs history function."""

    def __init__(self, max_size=6, initial_text=("",), store_blank=True):
        self.max_size = max_size
        self.store_blank = store_blank
        self.ls = gtk.ListStore(str)
        gtk.ComboBoxEntry.__init__(self, self.ls, 0)
        self.connect("notify::popup-shown", self.update_history)
        self.child.connect("activate", self.update_history)
        self.set_history("\x00".join(initial_text))
        geo = self.get_screen().get_root_window().get_geometry()
        cell = self.get_cells()[0]
        cell.props.wrap_width = geo[2] * 2 // 3
        cell.props.wrap_mode = pango.WRAP_CHAR

    def update_history(self, *args):
        text = self.child.get_text().strip()
        if self.store_blank or text:
            # Remove duplicate stored text.
            for i, row in enumerate(self.ls):
                if row[0] == text:
                    del self.ls[i]
            # Newly entered text goes at top of history.
            self.ls.prepend((text,))
            # History size is kept trimmed.
            if len(self.ls) > self.max_size:
                del self.ls[-1]
    
    def get_text(self):
        return self.child.get_text()

    def set_text(self, text):
        self.update_history()
        self.child.set_text(text)
        
    def get_history(self):
        self.update_history()
        return "\x00".join([row[0] for row in self.ls])

    def set_history(self, hist):
        self.ls.clear()
        for text in reversed(hist.split("\x00")):
            self.set_text(text)


class NamedTreeRowReference(object, metaclass=ABCMeta):
    """Provides named attribute access to gtk.TreeRowReference objects.
    
    This is a virtual base class.
    Virtual method 'get_index_for_name()' must be provided in a subclass.
    """

    def __init__(self, tree_row_ref):
        object.__setattr__(self, "_tree_row_ref", tree_row_ref)        
        
    @abstractmethod
    def get_index_for_name(self, tree_row_ref, name):
        """This method must be subclassed. Note the TreeRowReference
        in question is passed in in case that information is required
        to allocate the names.
        
        When a name is not available an exception must be raised and when
        one is the index into the TreeRowReference must be returned.
        """
        
        pass

    def _index_for_name(self, name):
        try:
            return self.get_index_for_name(self._tree_row_ref, name)
        except Exception:
            raise AttributeError("%s has no attribute: %s" %
                                            (repr(self._tree_row_ref), name))

    def __iter__(self):
        return iter(self._tree_row_ref)
        
    def __len__(self):
        return len(self._tree_row_ref)

    def __getitem__(self, path):
        return self._tree_row_ref[path]
        
    def __setitem__(self, path, data):
        self._tree_row_ref[path] = data

    def __getattr__(self, name):
        return self._tree_row_ref.__getitem__(self._index_for_name(name))

    def __setattr__(self, name, data):
        self._tree_row_ref[self._index_for_name(name)] = data

NamedTreeRowReference.register(list)


class WindowSizeTracker(object):
    """This class will monitor the un-maximized size of a window."""

    def __init__(self, window, tracking=True):
        self._window = window
        self._is_tracking = tracking
        self._x = self._y = 100
        self._max = False
        window.connect("configure-event", self._on_configure_event)
        window.connect("window-state-event", self._on_window_state_event)
        
    def set_tracking(self, tracking):
        self._is_tracking = tracking
        
    def get_tracking(self):
        return self._is_tracking

    def get_x(self):
        return self._x

    def get_y(self):
        return self._y

    def get_max(self):
        return self._max

    def get_text(self):
        """Marshalling function for save settings."""
        
        return json.dumps((self._x, self._y, self._max))
        
    def set_text(self, s):
        """Unmarshalling function for load settings."""
        
        try:
            self._x, self._y, self._max = json.loads(s)
        except Exception:
            pass

    def apply(self):
        self._window.unmaximize()
        self._window.resize(self._x, self._y)
        if self._max:
            idle_add(threadslock(self._window.maximize))

    def _on_configure_event(self, widget, event):
        if self._is_tracking and not self._max:
            self._x = event.width
            self._y = event.height

    def _on_window_state_event(self, widget, event): 
        if self._is_tracking:
            self._max = event.new_window_state & \
                                        gtk.gdk.WINDOW_STATE_MAXIMIZED != 0


class IconChooserButton(gtk.Button):
    """Imitate a FileChooserButton but specific to image types.
    
    The image rather than the mime-type icon is shown on the button.
    """
    
    __gsignals__ = {
            "filename-changed" : (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                                                    (gobject.TYPE_PYOBJECT,)),
    }
    
    def __init__(self, dialog):
        gtk.Button.__init__(self)
        dialog.set_icon_from_file(PGlobs.default_icon)

        hbox = gtk.HBox()
        hbox.set_spacing(4)
        image = gtk.Image()
        hbox.pack_start(image, False, padding=1)
        label = gtk.Label()
        label.set_alignment(0, 0.5)
        label.set_ellipsize(pango.ELLIPSIZE_END)
        hbox.pack_start(label)
        
        vsep = gtk.VSeparator()
        hbox.pack_start(vsep, False)
        rightmost_icon = gtk.image_new_from_stock(gtk.STOCK_OPEN,
                                                            gtk.ICON_SIZE_MENU)
        hbox.pack_start(rightmost_icon, False)
        self.add(hbox)
        hbox.show_all()

        self.connect("clicked", self._cb_clicked, dialog)
        self._dialog = dialog
        self._image = image
        self._label = label
        self.set_filename(dialog.get_filename())

    def set_filename(self, f):
        try:
            disp = glib.filename_display_name(f)
            pb = gtk.gdk.pixbuf_new_from_file_at_size(f, 16, 16)
        except (glib.GError, TypeError):
            # TC: Text reads as /path/to/file.ext or this when no file is chosen.
            self._label.set_text(_("(None)"))
            self._image.clear()
            self._filename = None
        else:
            self._label.set_text(disp)
            self._image.set_from_pixbuf(pb)
            self._filename = f
            self._dialog.set_filename(f)
        self.emit("filename-changed", self._filename)

    def get_filename(self):
        return self._filename

    def _cb_clicked(self, button, dialog):
        response = dialog.run()
        if response == gtk.RESPONSE_OK:
            self.set_filename(dialog.get_filename())
        elif response == gtk.RESPONSE_NONE:
            filename = self.get_filename()
            if filename is not None:
                dialog.set_filename(filename)
            self.set_filename(None)
        dialog.hide()

    def __getattr__(self, attr):
        if attr in gtk.FileChooser.__dict__:
            return getattr(self._dialog, attr)
        raise AttributeError("%s has no attribute, %s" % (
                                            self, attr))


class IconPreviewFileChooserDialog(gtk.FileChooserDialog):
    def __init__(self, *args, **kwds):
        gtk.FileChooserDialog.__init__(self, *args, **kwds)
        filefilter = gtk.FileFilter()
        # TC: the file filter text of a file chooser dialog.
        filefilter.set_name(_("Supported Image Formats"))
        filefilter.add_pixbuf_formats()
        self.add_filter(filefilter)

        vbox = gtk.VBox()
        frame = gtk.Frame()
        vbox.pack_start(frame, expand=True, fill=False)
        frame.show()
        image = gtk.Image()
        frame.add(image)
        self.set_use_preview_label(False)
        self.set_preview_widget(vbox)
        self.set_preview_widget_active(False)
        self.connect("update-preview", self._cb_update_preview, image)
        vbox.show_all()
        
    def _cb_update_preview(self, dialog, image):
        f = self.get_preview_filename()
        try:
            pb = gtk.gdk.pixbuf_new_from_file_at_size(f, 16, 16)
        except (glib.GError, TypeError):
            active = False
        else:
            active = True
            image.set_from_pixbuf(pb)
        self.set_preview_widget_active(active)


class LabelSubst(gtk.Frame):
    """User interface label substitution widget -- by the user."""

    def __init__(self, heading):
        gtk.Frame.__init__(self, " %s " % heading)
        self.vbox = gtk.VBox()
        self.vbox.set_border_width(2)
        self.vbox.set_spacing(2)
        self.add(self.vbox)
        self.textdict = {}
        self.activedict = {}

    def add_widget(self, widget, ui_name, default_text):
        frame = gtk.Frame(" %s " % default_text)
        frame.set_label_align(0.5, 0.5)
        frame.set_border_width(3)
        self.vbox.pack_start(frame)
        hbox = gtk.HBox()
        hbox.set_spacing(3)
        frame.add(hbox)
        hbox.set_border_width(2)
        use_supplied = gtk.RadioButton(None, _("Alternative"))
        use_default = gtk.RadioButton(use_supplied, _('Default'))
        self.activedict[ui_name + "_use_supplied"] = use_supplied
        hbox.pack_start(use_default, False)
        hbox.pack_start(use_supplied, False)
        entry = gtk.Entry()
        self.textdict[ui_name + "_text"] = entry
        hbox.pack_start(entry)
        
        if isinstance(widget, gtk.Frame):
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


class FolderChooserButton(gtk.Button):
    """Replaces the now-broken gtk.FileChosserButton for folder selection.
    
    The old chooser also had some issues with being able to visually select
    unmounted partitions that resulted in no change from the last valid
    selection. This button fixes that by dispensing with the drop down list
    entirely.
    
    In order to work properly this button's dialog must be in folder select
    mode.
    """

    __gsignals__ = { 'current-folder-changed' : (gobject.SIGNAL_RUN_FIRST,
        gobject.TYPE_NONE, (gobject.TYPE_STRING,))
    }

    def __init__(self, dialog=None):
        gtk.Button.__init__(self)
        self._current_folder = None
        self._handler_ids = []
        hbox = gtk.HBox()
        hbox.set_spacing(3)
        self.add(hbox)
        self._icon = gtk.image_new_from_stock(gtk.STOCK_DIRECTORY, gtk.ICON_SIZE_MENU)
        hbox.pack_start(self._icon, False)
        # TC: FolderChooserButton text for null -- no directory is set.
        self._label = gtk.Label(_("(None)"))
        self._label.set_alignment(0.0, 0.5)
        self._label.set_ellipsize(pango.ELLIPSIZE_END)
        hbox.pack_start(self._label)
        self._label.show()
        self.set_dialog(dialog)
        self.connect("clicked", self._on_clicked)
        self.get_child().show_all()

    def set_dialog(self, dialog):
        self._disconnect_from_dialog()

        if dialog is None:
            self._update_visual()
        else:
            self._connect_to_dialog(dialog)
            self.set_current_folder(dialog.get_current_folder())

    def get_dialog(self):
        return self._dialog

    def get_current_folder(self):
        return self._dialog and self._current_folder
        
    def set_current_folder(self, new_folder):
        """Call this, not the underlying dialog."""
        
        if new_folder is not None:
            new_folder = new_folder.strip()
            if new_folder != os.sep:
                new_folder = new_folder.rstrip(os.sep)
            
            if new_folder != self._current_folder:
                self._dialog.set_current_folder(new_folder)
                self.emit("current-folder-changed", new_folder)

    def unselect_all(self):
        self.set_current_folder("")

    def _update_visual(self):
        folder_name = self.get_current_folder()
        if not folder_name:
            folder_name = _("(None)")
        else:
            folder_name = os.path.split(folder_name)[1]
        self._label.set_text(folder_name)

    def _disconnect_from_dialog(self):
        for hid in self._handler_ids:
            self._dialog.handler_disconnect(hid)
        del self._handler_ids[:]
        self._dialog = None

    def _connect_to_dialog(self, dialog):
        app = self._handler_ids.append
        app(dialog.connect("destroy", self._on_dialog_destroy))
        self._dialog = dialog
        
    def _on_dialog_destroy(self, dialog):
        del self._handler_ids[:]
        self._dialog = None
        if not self.flags() & gtk.IN_DESTRUCTION:
            self._update_visual()

    def _on_clicked(self, button):
        if self._dialog is not None:
            self._dialog.set_current_folder(self._current_folder or "")
            if self._dialog.run() == gtk.RESPONSE_ACCEPT:
                new_folder = self._dialog.get_current_folder()
                if new_folder != self._current_folder:
                    self.emit('current-folder-changed', new_folder)
            else:
                self._dialog.set_current_folder(self._current_folder or "")
            self._dialog.hide()

    def do_current_folder_changed(self, new_folder):
        self._current_folder = new_folder
        self._update_visual()


def _source_wrapper(data):
    if data[0]:
        ret = data[1](*data[2], **data[3])
        if ret:
            return ret
        data[0] = False


def source_remove(data):
    if data[0]:
        glib.source_remove(data[4])
    data[0] = False


def timeout_add(interval, callback, *args, **kwargs):
    data = [True, callback, args, kwargs]
    data.append(glib.timeout_add(interval, _source_wrapper, data))
    return data


def timeout_add_seconds(interval, callback, *args, **kwargs):
    data = [True, callback, args, kwargs]
    data.append(glib.timeout_add_seconds(interval, _source_wrapper, data))
    return data


def idle_add(callback, *args, **kwargs):
    data = [True, callback, args, kwargs]
    data.append(glib.idle_add(_source_wrapper, data))
    return data


