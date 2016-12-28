"""The profile management dialog."""

#   Copyright (C) 2011, 2016 Stephen Fairchild
#   (s-fairchild@users.sourceforge.net)
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

__all__ = ["ProfileDialog"]

import atexit

# This is and needs to remain the initial gtk import point.
import glib
import gobject
import gtk
import pango

from idjc import PGlobs, FGlobs
from idjc.prelims import MAX_PROFILE_LENGTH, profile_name_valid, default
from ..utils import Singleton

gtk.gdk.threads_init()
gtk.gdk.threads_enter()
atexit.register(gtk.gdk.threads_leave)

from ..gtkstuff import ConfirmationDialog
from ..gtkstuff import ErrorMessageDialog
from ..gtkstuff import CellRendererLED
from ..gtkstuff import CellRendererTime
from ..gtkstuff import threadslock
from ..gtkstuff import IconChooserButton
from ..gtkstuff import IconPreviewFileChooserDialog
from ..gtkstuff import timeout_add


import gettext
t = gettext.translation(FGlobs.package_name, FGlobs.localedir, fallback=True)
_ = t.gettext



gtk.window_set_default_icon_from_file(PGlobs.default_icon)



class ProfileEntry(gtk.Entry):
    _allowed = (65056, 65361, 65363, 65365, 65288, 65289, 65535)
    
    def __init__(self):
        gtk.Entry.__init__(self)
        self.set_max_length(MAX_PROFILE_LENGTH)
        self.connect("key-press-event", self._cb_kp)
        self.connect("button-press-event", self._cb_button)
    
    
    def _cb_kp(self, widget, event):
        if not event.keyval in self._allowed and not \
                                    profile_name_valid(event.string):
            return True
            
            
    def _cb_button(self, widget, event):
        if event.button != 1:
            return True



class NewProfileDialog(gtk.Dialog):
    _icon_dialog = IconPreviewFileChooserDialog("Choose An Icon",
                        buttons = (gtk.STOCK_CLEAR, gtk.RESPONSE_NONE,
                                      gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                                      gtk.STOCK_OK, gtk.RESPONSE_OK))
    
    
    def __init__(self, row, filter_function=None, title_extra = "", edit=False):
        gtk.Dialog.__init__(self)
        self.set_border_width(6)
        self.get_child().set_spacing(12)
        self.set_modal(True)
        self.set_destroy_with_parent(True)
        self._icon_dialog.set_transient_for(self)
        
        if row is not None:
            if edit:
                # TC: data entry dialog window title text. %s = profile name
                title = _("Edit profile %s")
            else:
                # TC: data entry dialog window title text. %s = profile name
                title = _("New profile based upon %s")
            title %= row[1]
        else:
            # TC: data entry dialog window title text.
            title = _("New profile details")
        self.set_title(title + title_extra)

        hbox = gtk.HBox()
        hbox.set_border_width(6)
        hbox.set_spacing(12)
        if edit:
            icon = gtk.STOCK_EDIT
        else:
            icon = gtk.STOCK_COPY if row else gtk.STOCK_NEW
        self.image = gtk.image_new_from_stock(icon, gtk.ICON_SIZE_DIALOG)
        self.image.set_alignment(0.0, 0.0)
        hbox.pack_start(self.image, False)
        table = gtk.Table(2, 4)
        table.set_row_spacings(6)
        table.set_col_spacing(0, 6)
        hbox.pack_start(table)

        labels = (
                # TC: data entry dialog label text.
                _("Profile name"),
                # TC: data entry dialog label text.
                _("Icon"),
                # TC: data entry dialog label text.
                _("Nickname"),
                # TC: data entry dialog label text.
                _("Description"))
        names = ("profile_entry", "icon_button", "nickname_entry",
                                                        "description_entry")
        widgets = (ProfileEntry(), IconChooserButton(self._icon_dialog),
                      gtk.Entry(), gtk.Entry())

        for i, (label, name, widget) in enumerate(zip(labels, names, widgets)):
            label = gtk.Label(label)
            label.set_alignment(1.0, 0.5)
            table.attach(label, 0, 1, i, i + 1, gtk.SHRINK | gtk.FILL)

            table.attach(widget, 1, 2, i, i + 1, yoptions=gtk.SHRINK)
            setattr(self, name, widget)

        self.profile_entry.set_width_chars(30)
        self.get_content_area().add(hbox)
        bb = self.get_action_area()
        bb.set_spacing(6)

        if row is not None:
            profile = row[1] if edit else ""
            revert = gtk.Button(stock=gtk.STOCK_REFRESH)
            revert.connect("clicked", self._revert, row, edit)
            revert.clicked()
            bb.add(revert)
            bb.set_child_secondary(revert, True)
        else:
            self.icon_button.set_filename(PGlobs.default_icon)

        if edit:
            if self.profile_entry.get_text() == default:
                self.profile_entry.set_sensitive(False)
            self.delete = gtk.Button(stock=gtk.STOCK_DELETE)
            self.delete.connect_after("clicked", lambda w: self.destroy())
            bb.add(self.delete)
        cancel = gtk.Button(stock=gtk.STOCK_CANCEL)
        cancel.connect("clicked", lambda w: self.destroy())
        bb.add(cancel)
        self.ok = gtk.Button(stock=gtk.STOCK_OK)
        bb.add(self.ok)


    def _revert(self, widget, row, edit):
        profile_text = row[1] if edit else ""
        self.profile_entry.set_text(profile_text)
        self.icon_button.set_filename(row[4])
        self.nickname_entry.set_text(row[5])
        self.description_entry.set_text(row[2])
        self.profile_entry.grab_focus()


    @classmethod
    def append_dialog_title(cls, text):
        cls._icon_dialog.set_title(cls._icon_dialog.get_title() + text)
        


class ProfileSingleton(Singleton, type(gtk.Dialog)):
    def __call__(cls, *args, **kwds):
        return super(ProfileSingleton, cls).__call__(*args, **kwds)
    
    

class ProfileDialog(gtk.Dialog, metaclass=ProfileSingleton):
    __gproperties__ = {  "selection-active" : (gobject.TYPE_BOOLEAN, 
                                "selection active", 
                                "selected profile is active",
                                0, gobject.PARAM_READABLE),
                                
                                "selection": (str, "profile selection", 
                                "profile selected in profile manager",
                                "", MAX_PROFILE_LENGTH)
    }

    _signal_names = "choose", "delete", "auto"
    _new_profile_dialog_signal_names = "new", "clone", "edit"

    __gsignals__ = { "selection-active-changed" : (
                                gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                                (str, gobject.TYPE_BOOLEAN,)),
                                
                          "selection-changed" : (
                                gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                                (str,))
    }

    __gsignals__.update(dict(
            (x, (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,))) for x in (_signal_names)))

    __gsignals__.update(dict(
            (x, (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,) * 5))
            for x in (_new_profile_dialog_signal_names)))


    @property
    def profile(self):
        return self._profile


    def __init__(self, default, data_function=None):
        self._default = default
        self._profile = self._highlighted = None
        self._selection_active = False
        self._olddata = ()
        self._title_extra = ""

        # TC: profile dialog window title text.
        gtk.Dialog.__init__(self, _("IDJC Profile Manager"))
        self.set_size_request(600, 300)
        self.set_border_width(6)
        w = gtk.ScrolledWindow()
        w.set_border_width(6)
        w.set_shadow_type(gtk.SHADOW_ETCHED_OUT)
        w.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
        self.get_content_area().add(w)
        self.store = gtk.ListStore(
                            gtk.gdk.Pixbuf, str, str, int, str, str, int, int)
        self.sorted = gtk.TreeModelSort(self.store)
        self.sorted.set_sort_func(1, self._sort_func)
        self.sorted.set_sort_column_id(1, gtk.SORT_ASCENDING)
        self.treeview = gtk.TreeView(self.sorted)
        self.treeview.set_headers_visible(True)
        self.treeview.set_rules_hint(True)
        w.add(self.treeview)
        autorend = gtk.CellRendererPixbuf()
        autorend.props.width = 16
        autorend.props.stock_id = gtk.STOCK_APPLY
        autorend.props.stock_size = gtk.ICON_SIZE_MENU
        pbrend = gtk.CellRendererPixbuf()
        pbrend.props.width = 16
        strrend = gtk.CellRendererText()
        ledrend = CellRendererLED()
        time_rend = CellRendererTime()
        strrend_ellip = gtk.CellRendererText()
        strrend_ellip.props.ellipsize = pango.ELLIPSIZE_END
        # TC: column heading. The available profile names appears below.
        c0 = gtk.TreeViewColumn(None, autorend, visible=7)
        image = gtk.image_new_from_stock(gtk.STOCK_OPEN, gtk.ICON_SIZE_MENU)
        c0.set_widget(image)
        image.show()
        self.treeview.append_column(c0)
        c1 = gtk.TreeViewColumn(_("Profile"))
        c1.pack_start(pbrend, expand=False)
        c1.pack_start(strrend)
        c1.add_attribute(pbrend, "pixbuf", 0)
        c1.add_attribute(strrend, "text", 1)
        c1.set_spacing(2)
        self.treeview.append_column(c1)
        # TC: column heading. The profile nicknames.
        c2 = gtk.TreeViewColumn(_("Nickname"), strrend, text=5)
        self.treeview.append_column(c2)
        # TC: column heading.
        c3 = gtk.TreeViewColumn(_("Description"), strrend_ellip, text=2)
        c3.set_expand(True)
        self.treeview.append_column(c3)
        # TC: column heading. The time a particular profile has been running.
        c4 = gtk.TreeViewColumn(_("Up-time"))
        c4.pack_start(ledrend)
        c4.pack_start(time_rend)
        c4.add_attribute(ledrend, "active", 3)
        c4.add_attribute(time_rend, "time", 6)
        c4.set_spacing(2)
        self.treeview.append_column(c4)
        self.selection = self.treeview.get_selection()
        self.selection.connect("changed", self._cb_selection)
        box = self.get_action_area()
        box.set_spacing(6)
        for attr, label, sec, stock in zip(
                ("new", "clone", "edit", "delete", "auto", "cancel", "choose"), 
                (gtk.STOCK_NEW, gtk.STOCK_COPY, gtk.STOCK_EDIT,
                 gtk.STOCK_DELETE, _("_Auto"), gtk.STOCK_QUIT, gtk.STOCK_OPEN),
                (True,) * 4 + (False,) * 3,
                (True,) * 4 + (False,) + (True,) * 2):
            w = gtk.Button(label)
            w.set_use_stock(stock)
            box.add(w)
            box.set_child_secondary(w, sec)
            setattr(self, attr, w)

        self.delete.set_no_show_all(True)
        self.cancel.connect("clicked", self._cb_cancel)
        self.set_data_function(data_function)
        self.connect("notify::visible", self._cb_visible)
        for each in self._signal_names:
            getattr(self, each).connect("clicked", self._cb_click, each)
        for each in self._new_profile_dialog_signal_names:
            getattr(self, each).connect("clicked", self._cb_new_profile_dialog,
                                                                        each)
     
    
    def display_error(self, message, transient_parent=None, markup=False):
        error_dialog = ErrorMessageDialog("", message, markup=markup)
        error_dialog.set_transient_for(transient_parent or self)
        error_dialog.show_all()

    
    def destroy_new_profile_dialog(self):
        self._new_profile_dialog.destroy()
        del self._new_profile_dialog


    def get_new_profile_dialog(self):
        return self._new_profile_dialog
    
    
    def do_get_property(self, prop):
        if prop.name == "selection-active":
            return self._selection_active
        elif prop.name == "selection":
            return self._highlighted
        else:
            raise AttributeError("unknown property: %s" % prop.name)
    

    def do_selection_active_changed(self, profile, state):
        state = not state
        self.choose.set_sensitive(state)
        self.edit.set_sensitive(state)
        self.clone.set_sensitive(state)

    
    def _cb_click(self, widget, signal):
        if self._highlighted is not None:
            def commands():
                self.emit(signal, self._highlighted)
                self._update_data()

            if signal == "delete":
                if self._highlighted == self._default:
                    message = _("<span weight='bold' size='12000'>Delete the"
                            " data of profile '%s'?</span>\n\nThe profile will"
                            " remain available with initial settings.")
                else:
                    message = _("<span weight='bold' size='12000'>Delete "
                            "profile '%s' and all its data?</span>\n\nThe"
                            " data of deleted profiles cannot be recovered.")
                conf = ConfirmationDialog("", message % self._highlighted,
                                                                markup=True)
                conf.set_transient_for(self)
                conf.ok.connect("clicked", lambda w: commands())
                conf.show_all()
            else:
                commands()

    
    def _cb_new_profile_dialog(self, widget, action):
        if action in ("clone", "edit"):
            if self._highlighted is None:
                return
            row = self._get_row_for_profile(self._highlighted)
            template = row[1]
        else:
            row = None
            template = None
            
        np_dialog = self._new_profile_dialog = NewProfileDialog(row,
                    title_extra = self._title_extra, edit=action=="edit")
        np_dialog.set_transient_for(self)
        
        def sub_ok(widget):
            profile = np_dialog.profile_entry.get_text()
            icon = np_dialog.icon_button.get_filename()
            description = np_dialog.description_entry.get_text().strip()
            nickname = np_dialog.nickname_entry.get_text().strip()
            self.emit(action, profile, template, icon, nickname, description)
            self._update_data()
            self.highlight_profile(profile)
            
        np_dialog.ok.connect("clicked", sub_ok)
        if action == "edit":
            np_dialog.delete.connect("clicked", lambda w: self.delete.clicked())
        np_dialog.show_all()


    def _cb_cancel(self, widget):
        if self._profile is None:
            self.response(0)
        else:
            self.hide()


    def _cb_delete_event(self, widget, event):
        self.hide()
        return True

    
    def _cb_visible(self, *args):
        self._update_data()
        if self.props.visible:
            timeout_add(200, threadslock(self._update_data))
        
        
    def _cb_selection(self, ts):
        model, iter = ts.get_selected()
        if iter is not None:
            highlighted = model.get_value(iter, 1)
            active = model.get_value(iter, 3)
        else:
            highlighted = None
            active = False
        if highlighted != self._highlighted:
            self._highlighted = highlighted
            self.emit("selection-changed", self._highlighted)
        if active != self._selection_active:
            self._selection_active = active
            self.emit("selection-active-changed", self._highlighted, active)
                
        
    def highlight_profile(self, target, scroll=True):
        i = self._get_index_for_profile(target)
        if i is not None:
            self.selection.select_path(i)
            if scroll:
                self.selection.get_tree_view().scroll_to_cell(i)


    def _get_index_for_profile(self, target):
        for i, data in enumerate(self.sorted):
            if data[1] == target:
                return i
        return None


    def _get_row_for_profile(self, target):
        path = self._get_index_for_profile(target)
        if path is not None:
            return list(self.sorted[path])
        else:
            return None


    def _sort_func(self, model, *iters):
        vals = tuple(model.get_value(x, 1) for x in iters)
        
        try:
            return vals.index(self._default)
        except ValueError:
            return cmp(*vals)
        

    def set_data_function(self, f):
        self._data_function = f
        self._update_data()
        if f is not None:
            self.highlight_profile(self._default)


    def _auto_data_function(self, col, cell, model, iter):
        val = model.get_value(iter, 7)
        cell.set_visible(val)
        if val:
            cell.props.stock_id = gtk.STOCK_APPLY
            cell.props.stock_size = gtk.ICON_SIZE_MENU


    def _update_data(self):
        if self._data_function is not None:
            data = tuple(self._data_function())
            if self._olddata != data:
                self._olddata = data

                h = self._highlighted
                self.selection.handler_block_by_func(self._cb_selection)
                self.store.clear()
                for d in data:
                    if d["icon"] is not None:
                        i = d["icon"]
                    else:
                        if d["profile"] == self._default:
                            i = PGlobs.default_icon
                        else:
                            i = None
                    if i is not None:
                        try:
                            pb = gtk.gdk.pixbuf_new_from_file_at_size(i, 16, 16)
                        except glib.GError:
                            pb = i = None
                    else:
                        pb = None
                    desc = d["description"] or ""
                    active = d["active"]
                    nick = d["nickname"] or ""
                    uptime = d["uptime"]
                    auto = d["auto"]
                    self.store.append((pb, d["profile"], desc, active, i or "",
                                                            nick, uptime, auto))
                self.selection.handler_unblock_by_func(self._cb_selection)
                self.highlight_profile(h, scroll=False)
        return self.props.visible
        
        
    def set_profile(self, newprofile, title_extra, iconpathname):
        assert self._profile is None
        self.hide()
        self._profile = newprofile
        self.highlight_profile(newprofile, scroll=True)
        self.set_title(self.get_title() + title_extra)
        NewProfileDialog.append_dialog_title(title_extra)
        self._title_extra = title_extra
        try:
            self.set_icon_from_file(iconpathname)
        except glib.GError:
            print("Profile icon image file not found:", iconpathname)
        else:
            gtk.window_set_default_icon_from_file(iconpathname)

        self.cancel.set_label(gtk.STOCK_CLOSE)
        self.connect("delete-event", self._cb_delete_event)
        self.response(0)
        
        
    def run(self):
        if self._profile is None:
            self.show_all()
            gtk.Dialog.run(self)
        else:
            self.show()


    def present(self):
        self.show_all()
